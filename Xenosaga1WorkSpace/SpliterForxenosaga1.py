import os

def split_file_into_1gb(input_file):
    # 1GB를 바이트로 계산 (1024 * 1024 * 1024)
    chunk_size = 1024 * 1024 * 1024 
    
    if not os.path.exists(input_file):
        print(f"오류: {input_file} 파일을 찾을 수 없습니다.")
        return

    file_size = os.path.getsize(input_file)
    print(f"원본 파일 크기: {file_size:,} 바이트")

    with open(input_file, 'rb') as f:
        part_num = 1
        while True:
            # 1GB만큼 읽기
            chunk = f.read(chunk_size)
            if not chunk:
                break
            
            # 파트 파일명 생성 (예: data.bin -> data.bin.part1)
            output_name = f"{input_file}.part{part_num}"
            
            with open(output_name, 'wb') as out_f:
                out_f.write(chunk)
            
            print(f"저장 완료: {output_name} ({len(chunk):,} 바이트)")
            part_num += 1

    print("모든 파일 분할이 완료되었습니다.")

if __name__ == "__main__":
    # 여기에 분할할 파일 이름을 넣으세요
    target_file = "xenosaga1.big.new" 
    split_file_into_1gb(target_file)