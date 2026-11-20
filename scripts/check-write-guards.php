<?php declare(strict_types=1);
/* Run with PHP 8.4 and the path to the ZenTao source checkout. No database is required. */
$root = realpath($argv[1] ?? dirname(__DIR__, 2) . '/zentaopms');
if(!$root || !is_file("$root/framework/base/control.class.php"))
{
    fwrite(STDERR, "Usage: php check-write-guards.php /path/to/zentaopms\n");
    exit(2);
}
require "$root/framework/base/helper.class.php";
require "$root/framework/base/router.class.php";
require "$root/framework/base/control.class.php";

class helper extends baseHelper {}
class control extends baseControl {}
class browserFixture extends baseRouter { public $user; }
class api extends browserFixture {}
class writeGuardProbe extends baseControl
{
    public function check(): void { $this->requireWriteRequest(); }
}

function assertStatus(int $expected, callable $request, string $name): void
{
    $actual = 200;
    try { $request(); }
    catch(EndResponseException $e) { $actual = json_decode($e->getContent(), true, flags: JSON_THROW_ON_ERROR)['code']; }
    if($expected !== $actual) throw new RuntimeException("$name: expected HTTP $expected, got $actual");
}

session_start();
$app = (new ReflectionClass(browserFixture::class))->newInstanceWithoutConstructor();
$app->config = (object)array('sessionVar' => 'sid', 'framework' => (object)array('filterCSRF' => true));
$app->user   = (object)array('account' => 'fixture');
$lang = (object)array('error' => (object)array('accessDenied' => 'Forbidden'));
$_SERVER['HTTP_HOST']   = 'unit.test';
$_SERVER['HTTP_ORIGIN'] = 'http://unit.test';
$_COOKIE = array('sid' => session_id());

$actions = array(
    'todo'         => array('start' => [1], 'activate' => [1], 'finish' => [1], 'close' => [1], 'delete' => [1, 'yes'], 'batchFinish' => [], 'batchClose' => [], 'import2Today' => []),
    'backup'       => array('restore' => ['fixture'], 'delete' => ['fixture'], 'rmPHPHeader' => ['fixture']),
    'group'        => array('delete' => [1]),
    'space'        => array('deleteGroup' => [1], 'removeMember' => [1, 'demo']),
    'webhook'      => array('delete' => [1]),
    'entry'        => array('delete' => [1]),
    'user'         => array('unlock' => [1], 'unbind' => [1]),
    'my'           => array('unbind' => ['yes']),
    'project'      => array('unlinkMember' => [1, 1], 'unbindWhitelist' => [1], 'delete' => [1]),
    'execution'    => array('unlinkMember' => [1, 1], 'unbindWhitelist' => [1], 'delete' => [1, 'yes']),
    'program'      => array('unbindWhitelist' => [1], 'delete' => [1, 'yes']),
    'product'      => array('unbindWhitelist' => [1], 'delete' => [1, 'yes']),
    'productplan'  => array('delete' => [1]),
    'task'         => array('delete' => [1]),
    'personnel'    => array('unbindWhitelist' => [1]),
    'dept'         => array('delete' => [1]),
    'provider'     => array('delete' => [1]),
    'stakeholder'  => array('delete' => [1]),
    'cron'         => array('turnon' => [], 'delete' => [1], 'toggle' => [1, 'stop'], 'ajaxExec' => [true]),
);

foreach($actions as $module => $methods)
{
    require "$root/module/$module/control.php";
    $controller = (new ReflectionClass($module))->newInstanceWithoutConstructor();
    $controller->app  = $app;
    $controller->lang = $lang;
    foreach($methods as $method => $args)
    {
        foreach(array('GET' => 405, 'POST' => 403) as $verb => $expected)
        {
            $_SERVER['REQUEST_METHOD'] = $verb;
            assertStatus($expected, fn() => $controller->$method(...$args), "$verb $module.$method");
        }
    }
}

/* GET renders the password-verification dialog. POST must fail before loading the user. */
$userController = (new ReflectionClass(user::class))->newInstanceWithoutConstructor();
$userController->app  = $app;
$userController->lang = $lang;
$_SERVER['REQUEST_METHOD'] = 'POST';
assertStatus(403, fn() => $userController->delete(1), 'user.delete form requires token');

$probe = (new ReflectionClass(writeGuardProbe::class))->newInstanceWithoutConstructor();
$probe->app  = $app;
$probe->lang = $lang;
$_SERVER['REQUEST_METHOD'] = 'POST';
$_POST['csrfToken']        = helper::csrfToken();
assertStatus(200, $probe->check(...), 'valid browser form');
/* Exercise authorized restore, file deletion, and the independent scheduled backup using isolated data. */
class model { public $config; public $app; }
require "$root/module/backup/model.php";
if(!defined('DS')) define('DS', DIRECTORY_SEPARATOR);
class backupFixture extends backup
{
    public $backup;
    public $backupZen;
    public function useDirectory(string $path): void { $this->backupPath = $path . '/'; }
    public function send($data, string $type = 'json') { return $data; }
}

$backup = (new ReflectionClass(backupFixture::class))->newInstanceWithoutConstructor();
$backup->app    = $app;
$backup->config = (object)array('version' => '22.7', 'inQuickon' => false, 'backup' => (object)array('setting' => 'nofile'));
$backup->lang   = (object)array('saveSuccess' => 'saved', 'backup' => (object)array('success' => (object)array('restore' => 'restored', 'backup' => 'done')));
$backup->backupZen = new class
{
    public array $calls = array();
    public function restoreSQL($name): array { $this->calls[] = 'sql'; return array('result' => 'success'); }
    public function restoreFile($name): array { $this->calls[] = 'files'; return array('result' => 'success'); }
    public function backupSQL($name, $reload): array { $this->calls[] = 'backup'; return array('result' => 'success'); }
    public function removeExpiredFiles(): void { $this->calls[] = 'expiry'; }
};
$backupName = '20260101000000122_7';
$backupDir  = sys_get_temp_dir() . '/zentao-backup-guard-' . bin2hex(random_bytes(12));
if(!mkdir($backupDir, 0700)) throw new RuntimeException('Cannot create isolated backup directory');
$backupFile = "$backupDir/$backupName.sql";
try
{
    $backup->useDirectory($backupDir);
    $backup->config->backup->settingDir = $backupDir;
    $backup->backup = new backupModel();
    $backup->backup->config = $backup->config;
    $backup->backup->app = new class { public function getAppRoot(): string { return dirname(__DIR__, 2) . '/zentaopms/'; } };
    file_put_contents($backupFile, 'fixture');
    if($backup->restore($backupName)['result'] !== 'success') throw new RuntimeException('Authorized restore failed');
    $backup->delete($backupName);
    if(file_exists($backupFile)) throw new RuntimeException('Authorized deletion failed');
    $_SERVER['REQUEST_METHOD'] = 'GET';
    unset($_POST['csrfToken']);
    ob_start();
    $backup->backup('no', 'system');
    ob_end_clean();
    if($backup->backupZen->calls !== array('sql', 'files', 'backup', 'expiry')) throw new RuntimeException('Restore or scheduled backup workflow changed');
}
finally
{
    if(file_exists($backupFile)) unlink($backupFile);
    rmdir($backupDir);
}
$_SERVER['REQUEST_METHOD'] = 'POST';
$_POST['csrfToken'] = helper::csrfToken();
$_SERVER['HTTP_ORIGIN'] = 'https://other.test';
assertStatus(403, $probe->check(...), 'different origin');

$app = (new ReflectionClass(api::class))->newInstanceWithoutConstructor();
$app->config = $probe->app->config;
$app->user   = $probe->app->user;
$probe->app  = $app;
$_SERVER['REQUEST_METHOD'] = 'GET';
assertStatus(405, $probe->check(...), 'cookie-only API mutation');
$_SERVER['HTTP_TOKEN'] = 'ss_' . session_id();
assertStatus(200, $probe->check(...), 'explicitly authenticated API mutation');
echo "Write-request guard checks passed.\n";
