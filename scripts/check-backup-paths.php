<?php declare(strict_types=1);
/* Run with PHP 8.4, PDO SQLite and symlink support. All fixtures are temporary. */
$source = realpath($argv[1] ?? dirname(__DIR__, 2) . '/zentaopms');
if(!$source || !is_file("$source/module/backup/model.php"))
{
    fwrite(STDERR, "Usage: php check-backup-paths.php /path/to/zentaopms\n");
    exit(2);
}

define('DS', DIRECTORY_SEPARATOR);
class model { public object $config; public object $app; public PDO $dbh; public object $dao; }
require "$source/module/backup/model.php";
require "$source/lib/zdb/zdb.class.php";

function check(bool $ok, string $message): void
{
    if(!$ok) throw new RuntimeException($message);
}

function removeFixture(string $root): void
{
    $items = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS),
        RecursiveIteratorIterator::CHILD_FIRST
    );
    foreach($items as $item)
    {
        if($item->isLink() || !$item->isDir()) unlink($item->getPathname());
        else rmdir($item->getPathname());
    }
    rmdir($root);
}

$work = sys_get_temp_dir() . '/zentao-backup-check-' . bin2hex(random_bytes(12));
if(!mkdir($work, 0700)) throw new RuntimeException('Could not create fixture directory');
try
{
    $root = "$work/storage";
    $outside = "$work/other";
    mkdir($root);
    mkdir($outside);
    symlink($root, "$work/configured-volume");

    $old = '20200102030405';
    $current = '20261120123456722_7';
    file_put_contents("$root/$old.sql", "-- test fixture\n");
    file_put_contents("$root/$current.sql.php", "<?php die(); ?>\n-- test fixture\n");
    mkdir("$root/$current.file");
    mkdir("$root/$current.code");
    mkdir("$root/$current.file/documents");
    file_put_contents("$root/$current.file/documents/sample.txt", 'fixture');

    mkdir("$work/app/www/public", 0700, true);
    symlink("$work/app/www/public", "$work/web-alias");
    $dbh = new PDO('sqlite::memory:', null, null, [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
    $backup = new backupModel();
    $backup->dbh = $dbh;
    $backup->dao = new class { public function clearCache(): void {} };
    $backup->app = new class("$work/app/") {
        public int $databaseRequests = 0;
        public function __construct(private string $root) {}
        public function getAppRoot(): string { return $this->root; }
        public function loadClass(string $name): zdb
        {
            check($name === 'zdb', "Unexpected class requested: $name");
            ++$this->databaseRequests;
            return new class extends zdb {
                /* Adapt MySQL table discovery only; importStream and SQL execution are real. */
                public function getAllTables($type = 'base'): array
                {
                    return $this->dbh->query("SELECT name, type FROM sqlite_schema WHERE type = 'table'")->fetchAll(PDO::FETCH_KEY_PAIR);
                }
            };
        }
    };
    foreach([DS, "$work/app/www", "$work/app/www/public", "$work/web-alias"] as $forbidden)
    {
        check($backup->resolveBackupRoot($forbidden) === false, 'Accepted a system or web-facing backup root');
    }
    foreach([$root, "$work/configured-volume"] as $configured)
    {
        $backup->config = (object)['backup' => (object)['settingDir' => $configured]];
        foreach([[$old, 'sql', "$old.sql"], [$current, 'sql', "$current.sql.php"],
                 [$current, 'file', "$current.file"], [$current, 'code', "$current.code"]] as [$name, $type, $file])
        {
            $resolved = $backup->getBackupFile($name, $type);
            check(is_string($resolved) && realpath($resolved) === realpath("$root/$file"), "Failed to resolve $file via $configured");
        }
    }

    $protected = "$root/20200102030409.sql.php";
    $plain = "$root/20200102030409.sql";
    $content = "-- fixture comment
-- second line
";
    file_put_contents($protected, $content);
    check($backup->addFileHeader($protected), 'Could not protect the backup');
    $withHeader = file_get_contents($protected);
    check(str_starts_with($withHeader, '<?php die();') && str_ends_with($withHeader, $content), 'Header or SQL body was lost');
    check(!$backup->removeFileHeader($protected, "$outside/fixture.sql"), 'Accepted a backup output outside the configured root');
    check(file_get_contents($protected) === $withHeader, 'Failure modified the protected backup');
    check($backup->removeFileHeader($protected, $plain), 'Could not publish the unprotected backup');
    check(!file_exists($protected) && file_get_contents($plain) === $content, 'SQL did not round-trip intact');

    $malformed = "$root/20200102030410.sql.php";
    $malformedPlain = "$root/20200102030410.sql";
    file_put_contents($malformed, $content);
    check(!$backup->removeFileHeader($malformed, $malformedPlain), 'Accepted a missing protection header');
    check(file_get_contents($malformed) === $content && !file_exists($malformedPlain), 'Damaged a malformed backup');

    foreach(['sql', 'sql.php'] as $suffix)
    {
        $file = "$root/20200102030411.$suffix";
        $sql  = "CREATE TABLE restored (value TEXT);\n"
              . "INSERT INTO restored VALUES ('first $suffix');\n"
              . "INSERT INTO restored VALUES ('second $suffix');\n";
        file_put_contents($file, $sql);
        if($suffix === 'sql.php') check($backup->addFileHeader($file), 'Could not protect the restore fixture');
        $bytes = file_get_contents($file);
        $result = $backup->restoreSQL($file);
        check($result->result === true, "Restore failed for .$suffix: $result->error");
        $rows = $dbh->query('SELECT value FROM restored ORDER BY rowid')->fetchAll(PDO::FETCH_COLUMN);
        check($rows === ["first $suffix", "second $suffix"], "Incomplete restore for .$suffix");
        check(file_get_contents($file) === $bytes, "Restore modified .$suffix backup");
    }

    $databaseRequests = $backup->app->databaseRequests;
    foreach(['malformed' => "-- invalid protection header\n$sql", 'header-only' => "<?php die(); ?>\n"] as $label => $bytes)
    {
        file_put_contents($malformed, $bytes);
        check($backup->restoreSQL($malformed)->result === false, "Accepted $label protected backup");
        check($backup->app->databaseRequests === $databaseRequests, "Reached the database for $label backup");
        check(file_get_contents($malformed) === $bytes, "Modified $label protected backup");
    }
    check($dbh->query('SELECT value FROM restored ORDER BY rowid')->fetchAll(PDO::FETCH_COLUMN) === $rows, 'Invalid restore changed existing rows');

    file_put_contents("$outside/$old.sql", "-- unrelated fixture\n");
    check($backup->getBackupFile("../other/$old", 'sql') === false, 'Accepted a name outside the backup directory');
    file_put_contents("$root/$current.txt", 'fixture');
    check($backup->getBackupFile($current, 'txt') === false, 'Accepted an unsupported backup type');
    check($backup->getBackupFile($current, 'sql.php') === false, 'Accepted an internal SQL suffix as a public type');
    check($backup->getBackupFile('20200102030406', 'sql') === false, 'Accepted a missing backup');

    $linked = '20200102030407';
    symlink("$outside/$old.sql", "$root/$linked.sql");
    symlink($outside, "$root/$linked.code");
    check($backup->getBackupFile($linked, 'sql') === false, 'Accepted a symlink as a SQL backup');
    check($backup->getBackupFile($linked, 'code') === false, 'Accepted a symlink as a code backup');
    check($backup->isSafeBackupTree("$root/$current.file"), 'Rejected a plain attachment tree');
    $destination = "$work/restore";
    mkdir($destination);
    check($backup->isSafeBackupTree("$root/$current.file", $destination), 'Rejected a plain restore destination');
    symlink($outside, "$destination/documents");
    check(!$backup->isSafeBackupTree("$root/$current.file", $destination), 'Accepted a symlink in the restore destination');
    symlink($outside, "$root/$current.file/documents/linked");
    check(!$backup->isSafeBackupTree("$root/$current.file"), 'Accepted a nested directory symlink');
    echo "Backup path regression checks passed.\n";
}
finally
{
    removeFixture($work);
}
