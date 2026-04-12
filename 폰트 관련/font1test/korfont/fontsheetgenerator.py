import numpy as np
from PIL import Image, ImageDraw, ImageFont
import os

def generate_kor_font_sheet(ttf_path, output_name, char_list, font_size=18):
    # 1. 규격 설정 (기존 규격 유지)
    glyph_w, glyph_h = 20, 24
    grid_w, grid_h = 32, 32
    canvas_w = glyph_w * grid_w  # 640
    canvas_h = glyph_h * grid_h  # 768

    # 2. 캔버스 생성 ('L' 모드: 8비트 그레이스케일)
    canvas = Image.new('L', (canvas_w, canvas_h), color=0)
    draw = ImageDraw.Draw(canvas)
    
    try:
        # 실제 시스템에 설치된 폰트 경로로 설정되어 있어야 합니다.
        font = ImageFont.truetype(ttf_path, font_size)
    except:
        print(f"폰트 파일을 찾을 수 없습니다: {ttf_path}")
        return

    # 3. 글자 그리기
    for i, char in enumerate(char_list):
        if i >= grid_w * grid_h: break # 1024자 초과 방지
        
        row = i // grid_w
        col = i % grid_w
        
        x = col * glyph_w
        y = row * glyph_h
        
        # 여백 설정 (기존 설정 유지)
        fixed_off_x = 1
        fixed_off_y = 2
        
        draw.text((x + fixed_off_x, y + fixed_off_y), char, font=font, fill=255)

    # 4. 저장
    canvas.save(output_name)
    print(f"생성 완료: {output_name} (글자 수: {len(char_list[:1024])})")

if __name__ == "__main__":
    # 처리할 파일 접미사들
    suffixes = ['p1', 'p2', 'p3', 'p4']
    
    # 폰트 경로 설정 (사용자 환경에 맞게 수정 필요)
    # 기존 코드의 A예서체 경로를 기본값으로 두었습니다.
    target_font = r"ThinDungGeunMo.ttf"

    for s in suffixes:
        input_file = f"hangul_2048_final_{s}.txt"
        output_file = f"font0_{s}_kor_auto.png"
        
        if os.path.exists(input_file):
            with open(input_file, 'r', encoding='utf-8') as f:
                full_chars = f.read().strip()
            
            # 각 파일별로 시트 생성 (최대 1024자)
            generate_kor_font_sheet(
                ttf_path=target_font,
                output_name=output_file,
                char_list=full_chars,
                font_size=20
            )
        else:
            print(f"파일이 없습니다: {input_file}")