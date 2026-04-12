import numpy as np
from PIL import Image, ImageDraw, ImageFont

def generate_kor_font_sheet(ttf_path, output_name, char_list, font_size=18):
    # 1. 규격 설정 (일본어 폰트 시트와 동일)
    glyph_w, glyph_h = 20, 24
    grid_w, grid_h = 32, 32
    canvas_w = glyph_w * grid_w  # 640
    canvas_h = glyph_h * grid_h  # 768

    # 2. 캔버스 및 폰트 설정
    # 'L' 모드(8비트 그레이스케일)로 생성하여 나중에 비트 결합하기 좋게 만듭니다.
    canvas = Image.new('L', (canvas_w, canvas_h), color=0)
    draw = ImageDraw.Draw(canvas)
    
    try:
        font = ImageFont.truetype(r"C:\USERS\JO\APPDATA\LOCAL\MICROSOFT\WINDOWS\FONTS\A예서체.TTF", font_size)
    except:
        print("폰트 파일을 찾을 수 없습니다. 경로를 확인하세요.")
        return

    # 3. 글자 그리기
    for i, char in enumerate(char_list):
        if i >= grid_w * grid_h: break
        
        row = i // grid_w
        col = i % grid_w
        
        x = col * glyph_w
        y = row * glyph_h
        
        # [수정] bbox 기준 중앙 정렬 대신, 고정 좌표 사용
        # 폰트의 크기(font_size)에 맞춰 y_offset을 고정합니다.
        # 24픽셀 높이에서 20사이즈 폰트라면 보통 2~4픽셀 정도가 적당합니다.
        fixed_off_x = 1  # 좌측 여백 고정
        fixed_off_y = 2  # 상단 여백 고정 (이 값을 조정하며 높이를 맞추세요)
        
        # anchor="la" (Left-Ascent) 옵션을 사용하면 폰트의 기준선이 고정됩니다.
        draw.text((x + fixed_off_x, y + fixed_off_y), char, font=font, fill=255)

    # 4. 미리보기 및 저장
    print(f"시트 생성 완료: {output_name}")
    canvas.show() # 실행 시 이미지 미리보기 창을 띄웁니다.
    canvas.save(output_name)

if __name__ == "__main__":
    # 사용 예시
    # 이전에 생성했던 상위 2048자 리스트를 불러오거나 직접 입력하세요.
    with open('hangul_2048_final.txt', 'r', encoding='utf-8') as f:
        full_chars = f.read()

    # 1024자씩 끊어서 Blue, Green 채널 등에 넣을 용도로 만듭니다.
    # 예: Blue 채널용 (첫 1024자)
    generate_kor_font_sheet(
        ttf_path="C:/Windows/Fonts/batang.ttc", # 원하는 폰트 경로
        output_name="font0_blue_kor_auto.png",
        char_list=full_chars[:1024],
        font_size=20 # 20x24 규격에 맞게 적절히 조절
    )