@if (@X)==(@Y) @end /*
@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 949 >nul

rem ============================================================
rem 설정
rem ============================================================
set "ORIGINAL_DIR=E:\Xenosaga1Cutscenes\original"
set "SRT_TEMPLATE={CUTSCENENO}.srt"
set "MUX_TEMPLATE={CUTSCENENO}_KOR.mux"
set "CSCRIPT_EXE=%SystemRoot%\System32\cscript.exe"

pushd "%~dp0"
if errorlevel 1 (
    echo [오류 01] 배치 파일이 있는 폴더를 열 수 없습니다.
    pause
    exit /b 1
)

echo.
set "CUTSCENE="
set /p "CUTSCENE=컷신 번호를 입력하세요 (예: 2004D_2): "

if not defined CUTSCENE (
    echo.
    echo [오류 02] 컷신 번호가 입력되지 않았습니다.
    goto :FAIL
)

rem 영문자, 숫자, 밑줄, 하이픈 외 문자는 허용하지 않음
for /f "delims=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" %%A in ("%CUTSCENE%") do (
    echo.
    echo [오류 03] 컷신 번호에는 영문자, 숫자, 밑줄, 하이픈만 사용할 수 있습니다.
    goto :FAIL
)

set "TARGET_DIR=%CD%\%CUTSCENE%"
set "SRT_OUTPUT=%TARGET_DIR%\%CUTSCENE%.srt"
set "MUX_OUTPUT=%TARGET_DIR%\%CUTSCENE%_KOR.mux"

echo.
echo [1/5] 템플릿과 원본 폴더를 확인합니다...

if not exist "%SRT_TEMPLATE%" (
    echo [오류 11] SRT 템플릿을 찾을 수 없습니다.
    echo 경로: %CD%\%SRT_TEMPLATE%
    goto :FAIL
)

if not exist "%MUX_TEMPLATE%" (
    echo [오류 12] MUX 템플릿을 찾을 수 없습니다.
    echo 경로: %CD%\%MUX_TEMPLATE%
    goto :FAIL
)

if not exist "%ORIGINAL_DIR%\." (
    echo [오류 13] 원본 폴더를 찾을 수 없습니다.
    echo 경로: %ORIGINAL_DIR%
    goto :FAIL
)

if not exist "%CSCRIPT_EXE%" (
    echo [오류 14] cscript.exe를 찾을 수 없습니다.
    echo 경로: %CSCRIPT_EXE%
    goto :FAIL
)

echo [2/5] original의 모든 하위 폴더를 재귀 검색합니다...

set "ADS_SOURCE="
for /r "%ORIGINAL_DIR%" %%F in (%CUTSCENE%_vag_0.ads) do (
    if not defined ADS_SOURCE if exist "%%~fF" set "ADS_SOURCE=%%~fF"
)

set "M2V_SOURCE="
for /r "%ORIGINAL_DIR%" %%F in (%CUTSCENE%_video_0.m2v) do (
    if not defined M2V_SOURCE if exist "%%~fF" set "M2V_SOURCE=%%~fF"
)

if not defined ADS_SOURCE (
    echo.
    echo [오류 21] ADS 파일을 찾지 못했습니다.
    echo 검색 파일명: %CUTSCENE%_vag_0.ads
    echo 검색 시작점: %ORIGINAL_DIR%
    goto :FAIL
)

if not defined M2V_SOURCE (
    echo.
    echo [오류 22] M2V 파일을 찾지 못했습니다.
    echo 검색 파일명: %CUTSCENE%_video_0.m2v
    echo 검색 시작점: %ORIGINAL_DIR%
    goto :FAIL
)

echo.
echo [발견] ADS
echo %ADS_SOURCE%
echo.
echo [발견] M2V
echo %M2V_SOURCE%
echo.

echo [3/5] 컷신 폴더를 만들고 템플릿을 복사합니다...

if not exist "%TARGET_DIR%\." (
    md "%TARGET_DIR%"
    if errorlevel 1 (
        echo [오류 31] 컷신 폴더 생성에 실패했습니다.
        echo 경로: %TARGET_DIR%
        goto :FAIL
    )
)

copy /Y "%SRT_TEMPLATE%" "%SRT_OUTPUT%" >nul
if errorlevel 1 (
    echo [오류 32] SRT 템플릿 복사에 실패했습니다.
    goto :FAIL
)

copy /Y "%MUX_TEMPLATE%" "%MUX_OUTPUT%" >nul
if errorlevel 1 (
    echo [오류 33] MUX 템플릿 복사에 실패했습니다.
    goto :FAIL
)

echo [4/5] MUX 내부의 {CUTSCENENO}를 바이트 단위로 치환합니다...

"%CSCRIPT_EXE%" //nologo //E:JScript "%~f0" "%MUX_OUTPUT%" "%CUTSCENE%"
if errorlevel 1 (
    echo [오류 41] MUX 내부 치환에 실패했습니다.
    echo 파일: %MUX_OUTPUT%
    goto :FAIL
)

echo [5/5] ADS와 M2V를 컷신 폴더로 복사합니다...

copy /Y "%ADS_SOURCE%" "%TARGET_DIR%\%CUTSCENE%_vag_0.ads" >nul
if errorlevel 1 (
    echo [오류 51] ADS 파일 복사에 실패했습니다.
    echo 원본: %ADS_SOURCE%
    goto :FAIL
)

copy /Y "%M2V_SOURCE%" "%TARGET_DIR%\%CUTSCENE%_video_0.m2v" >nul
if errorlevel 1 (
    echo [오류 52] M2V 파일 복사에 실패했습니다.
    echo 원본: %M2V_SOURCE%
    goto :FAIL
)

echo.
echo ============================================
echo 완료
echo 폴더: %TARGET_DIR%
echo ============================================
echo %CUTSCENE%.srt
echo %CUTSCENE%_KOR.mux
echo %CUTSCENE%_vag_0.ads
echo %CUTSCENE%_video_0.m2v
echo.
popd
pause
exit /b 0

:FAIL
echo.
echo 작업이 중단되었습니다.
echo.
popd
pause
exit /b 1
*/

(function () {
    var args = WScript.Arguments;
    if (args.length < 2) {
        WScript.Echo("ERROR: missing arguments");
        WScript.Quit(10);
    }

    var filePath = args.Item(0);
    var replacementText = args.Item(1);
    var tokenText = "{CUTSCENENO}";

    function asciiBytes(text) {
        var textStream = new ActiveXObject("ADODB.Stream");
        textStream.Type = 2;
        textStream.Charset = "utf-8";
        textStream.Open();
        textStream.WriteText(text);
        textStream.Position = 0;
        textStream.Type = 1;
        textStream.Position = 3;
        var result = textStream.Read();
        textStream.Close();
        return result;
    }

    function toArray(variantBytes) {
        return new VBArray(variantBytes).toArray();
    }

    try {
        var input = new ActiveXObject("ADODB.Stream");
        input.Type = 1;
        input.Open();
        input.LoadFromFile(filePath);

        var raw = toArray(input.Read());
        var tokenVariant = asciiBytes(tokenText);
        var token = toArray(tokenVariant);
        var replacementVariant = asciiBytes(replacementText);

        var matches = [];
        var i, j, same;

        for (i = 0; i <= raw.length - token.length; i++) {
            same = true;
            for (j = 0; j < token.length; j++) {
                if (raw[i + j] !== token[j]) {
                    same = false;
                    break;
                }
            }
            if (same) {
                matches.push(i);
                i += token.length - 1;
            }
        }

        if (matches.length === 0) {
            input.Close();
            WScript.Echo("ERROR: token not found in MUX");
            WScript.Quit(11);
        }

        var output = new ActiveXObject("ADODB.Stream");
        output.Type = 1;
        output.Open();

        var sourcePosition = 0;
        for (i = 0; i < matches.length; i++) {
            var matchPosition = matches[i];
            var chunkLength = matchPosition - sourcePosition;

            if (chunkLength > 0) {
                input.Position = sourcePosition;
                output.Write(input.Read(chunkLength));
            }

            output.Write(replacementVariant);
            sourcePosition = matchPosition + token.length;
        }

        if (sourcePosition < input.Size) {
            input.Position = sourcePosition;
            output.Write(input.Read(input.Size - sourcePosition));
        }

        input.Close();
        output.SaveToFile(filePath, 2);
        output.Close();

        WScript.Echo("MUX token replacements: " + matches.length);
        WScript.Quit(0);
    } catch (e) {
        WScript.Echo("ERROR: " + e.number + " / " + e.description);
        WScript.Quit(12);
    }
})();
