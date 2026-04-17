import os
import shutil

# --- 설정 ---
LBA_ORIGIN_PATH = "LBA2.txt"
BIG_ORIGIN_PATH = "xenosaga2.big"
BIG_NEW_PATH = "xenosaga2.big.new"
LBA_NEW_PATH = "xenosaga.20_LBA_new.txt"
UNPACKED_DIR = "xenosaga2_UNPACKED"

def patch_archive():
    if not os.path.exists(UNPACKED_DIR):
        print(f"오류: {UNPACKED_DIR} 폴더가 없습니다.")
        return

    print("Step 0: 원본 아카이브 복제 중...")
    shutil.copy2(BIG_ORIGIN_PATH, BIG_NEW_PATH)

    # 원본 LBA 읽기 (ANSI)
    with open(LBA_ORIGIN_PATH, "r", encoding="cp949") as f:
        lba_lines = [line.strip() for line in f if line.strip()]

    # 새 아카이브 읽기/쓰기 모드 오픈
    f_new = open(BIG_NEW_PATH, "r+b")
    
    # 원본 순서 보존을 위한 리스트 복사 및 펜딩 딕셔너리
    final_lba_list = list(lba_lines)
    pending_updates = {}
    patched_count = 0

    print("Step 1: 아카이브 내 수정 및 패치 중 (섹터 여유분 계산 적용)...")

    for i, line in enumerate(lba_lines):
        parts = line.split('|')
        if len(parts) < 4: continue

        addr_hex, size_hex, index_hex, full_path = parts
        offset = int(addr_hex, 16)
        org_size = int(size_hex, 16)
        
        rel_path = full_path.lstrip('\\')
        target_path = os.path.join(UNPACKED_DIR, rel_path)

        if os.path.exists(target_path):
            with open(target_path, "rb") as pf:
                new_data = pf.read()
            new_size = len(new_data)

            # [핵심 로직] 현재 파일이 물리적으로 점유하고 있는 섹터의 최대 크기 계산 (0x800 단위)
            # 예: 원본 3000바이트 -> 섹터는 4096바이트 점유 중
            max_sector_size = (org_size + 2047) // 2048 * 2048
            if max_sector_size == 0 and org_size > 0: # 예외 처리
                max_sector_size = 2048

            # 수정된 파일이 기존 섹터 범위 내에 들어온다면 제자리 덮어쓰기
            if new_size <= max_sector_size:
                f_new.seek(offset)
                f_new.write(new_data)
                
                # 새로운 크기가 섹터 최대치보다 작다면 남은 공간은 00 패딩 (다음 파일 보호)
                if new_size < max_sector_size:
                    f_new.write(b'\x00' * (max_sector_size - new_size))
                
                final_lba_list[i] = f"{addr_hex}|{format(new_size, '08X')}|{index_hex}|{full_path}"
                print(f"  [STAY] 제자리 패치 (섹터 범위 내): {full_path} ({org_size} -> {new_size})")
                patched_count += 1
            else:
                # 섹터 범위를 완전히 벗어날 때만 아카이브 끝으로 이동
                pending_updates[i] = {
                    'data': new_data, 'size': new_size, 
                    'index': index_hex, 'path': full_path
                }
                print(f"  [MOVE] 섹터 범위 초과로 이동: {full_path}")
                patched_count += 1

    # Step 2: 섹터 범위를 초과한 파일들만 아카이브 끝에 추가
    if pending_updates:
        print("Step 2: 용량 초과 파일 추가 중...")
        f_new.seek(0, 2)
        
        for idx in pending_updates:
            item = pending_updates[idx]
            
            # 0x800 섹터 정렬 시작
            curr_pos = f_new.tell()
            aligned_start = (curr_pos + 2047) // 2048 * 2048
            if aligned_start > curr_pos:
                f_new.write(b'\x00' * (aligned_start - curr_pos))
            
            new_offset = f_new.tell()
            f_new.write(item['data'])
            
            # 파일 끝단 섹터 정렬 패딩
            curr_end = f_new.tell()
            aligned_end = (curr_end + 2047) // 2048 * 2048
            if aligned_end > curr_end:
                f_new.write(b'\x00' * (aligned_end - curr_end))
            
            final_lba_list[idx] = f"{format(new_offset, '08X')}|{format(item['size'], '08X')}|{item['index']}|{item['path']}"
            print(f"  [APPEND] {item['path']} -> {format(new_offset, '08X')}")

    f_new.close()

    # Step 3: LBA 텍스트 저장 (ANSI/CRLF/NUL 1개)
    with open(LBA_NEW_PATH, "wb") as f:
        content = "\r\n".join(final_lba_list)
        f.write(content.encode("cp949"))
        f.write(b"\r\n") # 마지막 줄바꿈
        f.write(b'\x00') # NUL 마커

    print(f"\n[작업 완료] 총 {patched_count}개 파일 반영됨.")

if __name__ == "__main__":
    patch_archive()