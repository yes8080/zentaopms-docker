<?php
/* Only the upload location in the hardened gateway supplies this FastCGI parameter. */
function denyPrivateUpload(): never
{
    while(ob_get_level() > 0) ob_end_clean();
    http_response_code(404);
    header_remove('Cache-Control'); // The gateway supplies private, no-store on every response.
    exit;
}

$uri = $_SERVER['ZENTAO_UPLOAD_URI'] ?? '';
if(!in_array($_SERVER['REQUEST_METHOD'] ?? '', ['GET', 'HEAD'], true)
    || !preg_match('~^/data/upload/([1-9][0-9]*)/([0-9]{6}/[A-Za-z0-9][A-Za-z0-9,_.-]{0,90})$~D', $uri, $path))
    denyPrivateUpload();

/* A legacy static image URL has no routing or authentication query parameters. */
$_GET = [];
$_REQUEST = [];
unset($_SERVER['HTTP_TOKEN']);
$_SERVER['QUERY_STRING'] = '';
ob_start();
try
{
    $appRoot = dirname(__DIR__);
    require "$appRoot/framework/router.class.php";
    require "$appRoot/framework/control.class.php";
    require "$appRoot/framework/model.class.php";
    require "$appRoot/framework/helper.class.php";

    $app = router::createApp('pms', $appRoot, 'router');
    if(!$app->checkInstalled() || $app->checkNeedUpgrade()) denyPrivateUpload();
    $common = $app->loadCommon();
    if(!$common->loadModel('user')->isLogon() || !$common->checkIP() || common::isTutorialMode()
        || !empty($app->user->modifyPassword) || (string)$app->company->id !== $path[1]) denyPrivateUpload();
    $common->checkMaintenance();

    $expectedPath = __DIR__ . $uri;
    if(realpath($expectedPath) !== $expectedPath || !is_file($expectedPath)) denyPrivateUpload();
    $files = $common->loadModel('file');
    /* Current uploads remove the extension on disk. Older files retain the database pathname. */
    $lookup = $common->dbh->prepare('SELECT * FROM ' . TABLE_FILE . ' WHERE pathname = ? OR pathname LIKE ? LIMIT 2');
    $lookup->execute([$path[2], str_replace('_', '\\_', $path[2]) . '.%']);
    $matches = $lookup->fetchAll(PDO::FETCH_OBJ);
    if(count($matches) !== 1) denyPrivateUpload();
    $file = $matches[0];
    if($file->deleted || in_array($file->objectType, ['traincourse', 'traincontents'], true)) denyPrivateUpload();
    $files->setFileWebAndRealPaths($file);
    if($file->realPath !== $expectedPath) denyPrivateUpload();

    /* Types with no known container ACL use their native application entry, never a raw URL. */
    if($file->objectID && !isset($app->config->file->objectGroup[$file->objectType])
        && !in_array($file->objectType, ['program', 'project', 'product', 'execution', 'doc', 'build', 'release', 'feedback'], true)) denyPrivateUpload();
    if(!$files->checkPriv($file)) denyPrivateUpload();

    $extension = strtolower($file->extension);
    $inlineTypes = ['jpg' => 'image/jpeg', 'jpeg' => 'image/jpeg', 'png' => 'image/png', 'gif' => 'image/gif',
        'bmp' => 'image/bmp', 'webp' => 'image/webp', 'mp4' => 'video/mp4'];
    $preview = isset($inlineTypes[$extension]);
    if($file->objectType === 'doc' && !common::hasPriv('doc', 'view')) denyPrivateUpload();
    if(!$preview && $file->objectType !== 'doc' && !common::hasPriv('file', 'download')) denyPrivateUpload();

    session_write_close();
    ob_end_clean();
    header_remove('Cache-Control');
    header('Content-Type: ' . ($inlineTypes[$extension] ?? 'application/octet-stream'));
    if(!$preview) header('Content-Disposition: attachment');
    header('X-Accel-Redirect: /_zentao_files/' . $path[1] . '/' . $path[2]);
}
catch(Throwable $error)
{
    error_log('Private upload authorization failed (' . get_class($error) . ').');
    denyPrivateUpload();
}
