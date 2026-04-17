@echo off
@chcp 65001
python ArchivePatchTool2.py
XenoLbar.exe xenosaga.20 xenosaga.20_LBA_new.txt xenosaga.20.new
python SpliterForxenosaga2.py
ren "xenosaga2.big.new.part1" "xenosaga.21.new"
ren "xenosaga2.big.new.part2" "xenosaga.22.new"
ren "xenosaga2.big.new.part3" "xenosaga.23.new"
ren "xenosaga2.big.new.part4" "xenosaga.24.new"
echo xenosaga2 Repacking Complete
pause