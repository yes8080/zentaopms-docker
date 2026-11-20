<?php
// Apply the policy after route parsing, before controller resolution and dispatch.
$file = '/var/www/zentao/framework/base/router.class.php';
$policy = <<<'PHP'
    private function enforceHardenedRoute(): void
    {
        $module = strtolower((string)$this->moduleName);
        $method = strtolower((string)$this->methodName);
        if(in_array($module, array('editor', 'dev'), true)
            || ($module === 'extension' && !in_array($method, array('browse', 'structure'), true))
            || ($module === 'chart' && in_array($method, array('ajaxgetchart', 'ajaxgetfilterform'), true))
            || ($module === 'user' && $method === 'login' && (isset($_GET['account']) || isset($_GET['password']))))
        {
            http_response_code(403);
            exit;
        }
    }

PHP;

$source = file_get_contents($file);
$guard = "        \$this->enforceHardenedRoute();\n";
$insertions = array(
    "class baseRouter\n{\n" => $policy . "\n",
    "    public function setControlFile(bool \$exitIfNone = true)\n    {\n" => $guard,
    "    public function loadModule()\n    {\n" => $guard,
);
foreach($insertions as $signature => $insertion)
{
    $source = str_replace($signature, $signature . $insertion, $source, $matches);
    if($matches !== 1)
    {
        fwrite(STDERR, "Could not apply the hardened route policy.\n");
        exit(1);
    }
}
if(file_put_contents($file, $source) !== strlen($source)) exit(1);
