import os
import shutil

# --- 설정 ---
LBA_ORIGIN_PATH = "LBA0.txt"
BIG_ORIGIN_PATH = "xenosaga0.big"
BIG_NEW_PATH = "xenosaga0.big.new"
LBA_NEW_PATH = "xenosaga.00_LBA_new.txt"
UNPACKED_DIR = "xenosaga0_UNPACKED"

def patch_archive():
    if not os.path.exists(UNPACKED_DIR):
        print(f"오류: {UNPACKED_DIR} 폴더가 없습니다.")
        return

    print("Step 0: 원본 아카이브 복제 중...")
    shutil.copy2(BIG_ORIGIN_PATH, BIG_NEW_PATH)

    with open(LBA_ORIGIN_PATH, "r", encoding="cp949") as f:
        lba_lines = [line.strip() for line in f if line.strip()]

    f_new = open(BIG_NEW_PATH, "r+b")
    
    # 1. 원본 순서를 그대로 유지하기 위해 리스트 전체를 미리 복사
    final_lba_list = list(lba_lines)
    # 크기가 커서 나중에 기록할 파일들을 담아둘 딕셔너리 (인덱스 유지용)
    pending_updates = {}
    patched_count = 0

    print("Step 1: 아카이브 내 수정 및 패치 중...")

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

            if new_size <= org_size:
                # 제자리 교체
                f_new.seek(offset)
                f_new.write(new_data)
                if new_size < org_size:
                    f_new.write(b'\x00' * (org_size - new_size))
                # 리스트 순서(i) 그대로 값만 업데이트
                final_lba_list[i] = f"{addr_hex}|{format(new_size, '08X')}|{index_hex}|{full_path}"
                print(f"  [OK] 제자리 교체: {full_path}")
            else:
                # 크기 초과 -> 기존 자리는 00 처리
                f_new.seek(offset)
                f_new.write(b'\x00' * org_size)
                # 나중에 한꺼번에 기록하기 위해 펜딩 (인덱스 i를 키로 사용)
                pending_updates[i] = {
                    'data': new_data, 'size': new_size, 
                    'index': index_hex, 'path': full_path
                }
                print(f"  [WAIT] EOF 이동 대기: {full_path}")
            patched_count += 1

    # Step 2: 용량 초과 파일들 아카이브 끝에 추가
    if pending_updates:
        print("Step 2: 용량 초과 파일 추가 중...")
        f_new.seek(0, 2) # 파일 실제 끝
        
        # 펜딩된 파일들을 순차적으로 기록
        for idx in pending_updates:
            item = pending_updates[idx]
            
            # 섹터 정렬 (0x800)
            curr_pos = f_new.tell()
            aligned_start = (curr_pos + 2047) // 2048 * 2048
            if aligned_start > curr_pos:
                f_new.write(b'\x00' * (aligned_start - curr_pos))
            
            new_offset = f_new.tell()
            f_new.write(item['data'])
            
            # 다음 파일 보호를 위한 끝단 정렬
            curr_end = f_new.tell()
            aligned_end = (curr_end + 2047) // 2048 * 2048
            if aligned_end > curr_end:
                f_new.write(b'\x00' * (aligned_end - curr_end))
            
            # **가장 중요한 부분**: 원본 리스트의 '정확히 그 위치(idx)'를 찾아가서 내용물만 교체
            final_lba_list[idx] = f"{format(new_offset, '08X')}|{format(item['size'], '08X')}|{item['index']}|{item['path']}"
            print(f"  [APPEND] {item['path']} -> {format(new_offset, '08X')}")

    # Step 3: 아카이브 끝에 더미 추가
    f_new.seek(0, 2)
    f_new.write(b'\x00' * 2048)
    f_new.close()

    # Step 4: LBA 텍스트 저장 (CR LF 적용, ANSI 인코딩, NUL 1개 추가)
    with open(LBA_NEW_PATH, "wb") as f:
        # 위에서 데이터가 쌓인 변수 이름(final_lba_list)을 사용하여 줄바꿈 처리
        # \r\n을 사용하여 윈도우 표준 CR LF 적용
        content = "\r\n".join(final_lba_list)
        
        # 1. 텍스트 데이터를 ANSI(cp949)로 인코딩하여 저장
        f.write(content.encode("cp949"))

    print(f"\n[최종 성공] {LBA_NEW_PATH}가 ANSI/CRLF/NUL 1개 형식으로 저장되었습니다.")

    print(f"\n[성공] 리스트 순서를 100% 보존하며 {patched_count}개 패치 완료.")

if __name__ == "__main__":
    patch_archive()