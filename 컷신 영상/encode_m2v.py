import sys
import os
import subprocess
import json

def main():
    if len(sys.argv) != 3:
        print("사용법: python encode_m2v.py <편집본.mpg> <원본_video.m2v>")
        return

    input_video = sys.argv[1]
    orig_m2v    = sys.argv[2]
    output_m2v  = os.path.splitext(input_video)[0] + "_fixed.m2v"

    if not os.path.exists(input_video):
        print(f"오류: {input_video} 없음"); return
    if not os.path.exists(orig_m2v):
        print(f"오류: {orig_m2v} 없음"); return

    # 원본 m2v 크기
    orig_size = os.path.getsize(orig_m2v)
    print(f"[*] 원본 m2v 크기: {orig_size:,} bytes")

    # 입력 영상 duration
    r = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-select_streams', 'v:0',
         '-show_entries', 'stream=duration', '-of', 'json', input_video],
        capture_output=True, text=True
    )
    data = json.loads(r.stdout)
    duration = float(data['streams'][0]['duration'])
    print(f"[*] 입력 영상 duration: {duration:.3f}초")

    # 목표 비트레이트 (원본보다 1% 작게)
    target_kbps = int(orig_size * 8 / duration * 0.97 / 1000)
    print(f"[*] 목표 비트레이트: {target_kbps} kbps")

    # ffmpeg 인코딩
    print(f"[*] 인코딩 시작: {input_video} -> {output_m2v}")
    cmd = [
        'ffmpeg', '-y', '-i', input_video,
        '-vcodec', 'mpeg2video',
        '-s', '512x448',
        '-r', '30000/1001',
        '-b:v', f'{target_kbps}k',
        '-maxrate', f'{target_kbps}k',
        '-minrate', f'{target_kbps}k',
        '-bufsize', '1835k',
        '-bf', '2', '-g', '18',
        '-profile:v', 'main', '-level:v', 'main',
        '-pix_fmt', 'yuv420p',
        '-an', '-f', 'mpeg2video', output_m2v
    ]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print("오류: ffmpeg 인코딩 실패"); return

    # sequence end code 확인 및 추가
    with open(output_m2v, 'r+b') as f:
        f.seek(-4, 2)
        tail = f.read(4)
        if tail != bytes.fromhex('000001b7'):
            f.write(bytes.fromhex('000001b7'))
            print("[*] sequence end code 추가됨")
        else:
            print("[*] sequence end code 이미 있음")

    # 크기 비교
    new_size = os.path.getsize(output_m2v)
    diff = new_size - orig_size
    print(f"[*] 원본 m2v: {orig_size:,} bytes")
    print(f"[*] 새  m2v:  {new_size:,} bytes")
    print(f"[*] 차이:     {diff:+,} bytes ({diff/orig_size*100:+.2f}%)")
    if abs(diff) < orig_size * 0.005:
        print("[+] 크기 허용 범위 내")
    else:
        print("[!] 크기 차이 큼 - 비트레이트 재조정 필요할 수 있음")

    print(f"\n[+] 완료: {output_m2v}")
    print("이제 ps2strw로 mux하세요.")

if __name__ == "__main__":
    main()
