<?php
/* Apply the same attachment checks to ZenTao's normal read/download paths and the legacy URL adapter. */
$model = '/var/www/zentao/module/file/model.php';
$old = <<<'PHP'
        if(!$file->objectType || !$file->objectID) return true;
        if(!$this->loadModel('user')->isLogon()) return true;
        if($file->extra == 'editor') return true;
PHP;
$new = <<<'PHP'
        if(!empty($file->deleted) || !$this->loadModel('user')->isLogon()) return false;

        if(!$file->objectType || !$file->objectID)
        {
            if($file->addedBy === $this->app->user->account) return true;
            if($file->objectType !== 'avatar' || !in_array($file->extension, $this->config->file->imageExtensions, true)) return false;

            /* Other users can see an avatar only after its uploader publishes it on their profile. */
            $this->setFileWebAndRealPaths($file);
            return (bool)$this->dao->select('id')->from(TABLE_USER)->where('account')->eq($file->addedBy)
                ->andWhere('avatar')->eq($file->webPath)->andWhere('deleted')->eq('0')->fetch('id');
        }

        if(in_array($file->objectType, ['program', 'project', 'product', 'execution'], true))
            return $this->loadModel($file->objectType)->checkPriv((int)$file->objectID);

        /* Bound editor images continue through the same owning-object checks as ordinary files. */
PHP;
function replaceRequired(string $source, string $old, string $new, int $expected): string
{
    $source = str_replace($old, $new, $source, $matches);
    if($matches !== $expected) throw new RuntimeException('Could not apply the hardened file policy.');
    return $source;
}
$source = replaceRequired(file_get_contents($model), $old, $new, 1);
$downloadHeader = "        helper::header('Pragma', 'no-cache');";
$source = replaceRequired($source, $downloadHeader, "        helper::header('Cache-Control', 'private, no-store');\n" . $downloadHeader, 1);
if(file_put_contents($model, $source) !== strlen($source)) exit(1);

$control = '/var/www/zentao/module/file/control.php';
$old = <<<'PHP'
        $cacheMaxAge = 10 * 365 * 24 * 3600;
        helper::header('Cache-Control', "private, max-age=$cacheMaxAge");
        helper::header('Pragma', 'cache');
        helper::header('Expires', gmdate('D, d M Y H:i:s', time() + $cacheMaxAge) . ' GMT');
PHP;
$new = "        helper::header('Cache-Control', 'private, no-store');";
$source = replaceRequired(file_get_contents($control), $old, $new, 2);
if(file_put_contents($control, $source) !== strlen($source)) exit(1);
