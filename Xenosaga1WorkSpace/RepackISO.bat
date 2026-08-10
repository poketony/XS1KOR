@echo off
python main.py repack
for /f "delims=" %%f in ('dir /b /o-d "kansei\*.iso"') do (
    move "kansei\%%f" "%USERPROFILE%\Desktop\%%f"
    goto done
)
:done
rd /s /q "kansei\repack00"
rd /s /q "kansei\repack10"
rd /s /q "kansei\repack20"
pause