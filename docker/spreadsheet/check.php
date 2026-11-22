<?php
declare(strict_types=1);

require '/var/www/zentao/lib/phpexcel/phpexcel.class.php';
if (Composer\InstalledVersions::getPrettyVersion('phpoffice/phpspreadsheet') !== '1.30.8') {
    throw new RuntimeException('The application loaded unexpected spreadsheet metadata.');
}
foreach (['Xls', 'Xlsx'] as $format) {
    $file = tempnam(sys_get_temp_dir(), 'zentao-sheet-');
    try {
        $book = new phpExcel();
        $sheet = $book->getActiveSheet();
        $sheet->setTitle('项目');
        $sheet->setCellValueExplicit('A1', '任务001', PhpOffice\PhpSpreadsheet\Cell\DataType::TYPE_STRING);
        $sheet->setCellValue('B2', 23);
        $sheet->setCellValue('C2', '=B2+1');
        $sheet->mergeCells('A3:B3');
        $book->createWriter($format)->save($file);
        if (!phpExcel::canRead($file)) throw new RuntimeException("ZenTao cannot recognise $format output.");
        $read = phpExcel::load($file)->getSheetByName('项目');
        if ($read === null || $read->getCell('A1')->getValue() !== '任务001' || (float)$read->getCell('C2')->getCalculatedValue() !== 24.0 || !$read->getCell('B3')->isInMergeRange()) {
            throw new RuntimeException("ZenTao $format round-trip failed.");
        }
    } finally {
        unlink($file);
    }
    echo "$format write/read passed\n";
}
