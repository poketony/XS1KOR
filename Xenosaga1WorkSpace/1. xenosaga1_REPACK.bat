@echo off
@chcp 65001
python ArchivePatchTool1.py
XenoLbar.exe xenosaga.10 xenosaga.10_LBA_new.txt xenosaga.10.new
python SpliterForxenosaga1.py
ren "xenosaga1.big.new.part1" "xenosaga.11.new"
ren "xenosaga1.big.new.part2" "xenosaga.12.new"
ren "xenosaga1.big.new.part3" "xenosaga.13.new"
echo xenosaga1 Repacking Complete
pause