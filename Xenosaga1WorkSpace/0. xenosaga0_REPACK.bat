@echo off
@chcp 65001
python ArchivePatchTool.py
XenoLbar.exe xenosaga.00 xenosaga.00_LBA_new.txt xenosaga.00.new
python SpliterForxenosaga0.py
ren "xenosaga0.big.new.part1" "xenosaga.01.new"
ren "xenosaga0.big.new.part2" "xenosaga.02.new"
echo xenosaga0 Repacking Complete
pause