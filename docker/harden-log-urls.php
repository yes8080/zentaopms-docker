<?php
// Keep a bounded entry-point name in both database logs, even before route parsing.
$entryMethod = <<<'PHP'
    public function getLogEntryName(): string
    {
        $script = basename((string)($_SERVER['SCRIPT_FILENAME'] ?? ''));
        if(in_array($script, array('index.php', 'api.php', 'x.php', 'imgproxy.php'), true)) return $script;
        return PHP_SAPI === 'cli' ? 'cli' : 'unknown';
    }

PHP;

$routerHeader = "class baseRouter\n{\n";
$patches = array(
    'framework/base/router.class.php' => array(
        $routerHeader => $routerHeader . $entryMethod . "\n",
        'substr((string)$this->getURI(), 0, 255), helper::now()' => '$this->getLogEntryName(), helper::now()',
    ),
    'module/common/model.php' => array(
        "\$this->loadModel('entry')->saveLog(\$entry->id, \$this->server->request_uri);" =>
        "\$this->loadModel('entry')->saveLog(\$entry->id, \$this->app->getLogEntryName());",
    ),
);
foreach($patches as $file => $changes)
{
    $source = file_get_contents($file);
    if($source === false) exit(1);
    foreach($changes as $before => $after)
    {
        $source = str_replace($before, $after, $source, $count);
        if($count !== 1)
        {
            fwrite(STDERR, "Unexpected source in $file. Review the log redaction for this release.\n");
            exit(1);
        }
    }
    if(file_put_contents($file, $source) !== strlen($source)) exit(1);
}
