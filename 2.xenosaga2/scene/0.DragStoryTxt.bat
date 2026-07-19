@echo off
@chcp 65001 >nul
setlocal enabledelayedexpansion

for %%f in (%*) do (
    if /i "%%~xf" == ".txt" (
        :: 1. 확장자 .txt를 제거한 순수 파일명 추출 (예: ST0259.evt)
        set "TARGET=%%~dpnf"
        
        echo.
        echo 대상 파일: %%~nxf
        
        :: 2. 추출된 이름(ST0259.evt)이 실제 존재하는지 확인
        if exist "!TARGET!" (
            echo 명령 실행: python xeno_evt.py "!TARGET!" "%%~ff"
            python xeno_evt.py "!TARGET!" "%%~ff"
        ) else (
            echo [경고] "!TARGET!" 파일을 찾을 수 없습니다.
        )
    )
)
pause