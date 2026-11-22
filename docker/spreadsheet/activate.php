<?php
declare(strict_types=1);

// The application has customised Composer mappings for unrelated packages.
// Keep that loader and register a separately locked spreadsheet dependency set.
$vendor = '/var/www/zentao/lib/vendor';
$overlay = '/opt/zentao-spreadsheet/vendor';
$metadata = $vendor . '/composer/installed.json';
$installed = json_decode(file_get_contents($metadata), true, 512, JSON_THROW_ON_ERROR);
$versions = array_column($installed['packages'], 'version', 'name');
foreach (['phpoffice/phpspreadsheet' => '1.8.2', 'ezyang/htmlpurifier' => '4.18.0', 'psr/simple-cache' => '1.0.1'] as $name => $version) {
    if (ltrim($versions[$name] ?? '', 'v') !== $version || !is_dir("$vendor/$name")) {
        throw new RuntimeException("Review spreadsheet dependencies for this ZenTao source: $name");
    }
}
$names = ['phpoffice/phpspreadsheet', 'markbaker/complex', 'markbaker/matrix'];
foreach (array_slice($names, 1) as $name) {
    if (file_exists("$vendor/$name")) throw new RuntimeException("Review the existing $name directory before upgrading.");
}
// The snapshot lists two math libraries without shipping their directories.
// The overlay owns their actual files, versions and autoload rules, along with PhpSpreadsheet.
$installed['packages'] = array_values(array_filter($installed['packages'], static fn($pkg) => !in_array($pkg['name'], $names, true)));
file_put_contents($metadata, json_encode($installed, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n");
$metadata = $vendor . '/composer/installed.php';
$installed = require $metadata;
foreach ($names as $name) unset($installed['versions'][$name]);
file_put_contents($metadata, "<?php return " . var_export($installed, true) . ";\n");
rename("$vendor/autoload.php", "$vendor/autoload-zentao.php");
file_put_contents("$vendor/autoload.php", <<<'PHP'
<?php
$zentaoLoader = require __DIR__ . '/autoload-zentao.php';
require_once '/opt/zentao-spreadsheet/vendor/autoload.php';
return $zentaoLoader;
PHP);
// Preserve direct paths used by code or administrators for package resources.
$old = "$vendor/phpoffice/phpspreadsheet";
rename($old, $argv[1] . '/old-spreadsheet');
symlink("$overlay/phpoffice/phpspreadsheet", $old);
