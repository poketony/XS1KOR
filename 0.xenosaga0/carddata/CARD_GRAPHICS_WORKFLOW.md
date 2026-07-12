# 카드 그래픽 작업 방법

CMD를 이 `carddata` 폴더에서 연 뒤 아래 명령을 사용한다.

```bat
cardgfx list
cardgfx extract-all
cardgfx extract CARDGRAP
cardgfx extract help\h_text
cardgfx rebuild CARDGRAP
cardgfx rebuild help\h_text
```

`list`는 실제 매직이 XTX/ARX인 리소스, 추출 여부, 자동 선택된 LEX를 표시한다.
`extract-all`은 전체 리소스를 추출하고, `extract`는 지정한 파일 하나만 다시
추출한다. 파일명만으로 중복되면 `help\h_text`처럼 상대 경로를 입력한다.

추출물은 원래 하위 경로를 유지한 채 `graphics_extract`에 저장된다. 리빌드
파일은 `graphics_rebuilt` 아래에 원본과 같은 파일명으로 생성된다. 출력
루트가 분리되어 있으므로 원본 XTX/BIN 파일은 덮어쓰지 않는다.

추출된 원본 PNG는 그대로 두고, 수정본 이름에는 확장자 앞에 `_KOR`를 붙인다.

```text
PSMT8_001.png       -> PSMT8_001_KOR.png
PSMT4_001.png       -> PSMT4_001_KOR.png
PSMT8_card.png      -> PSMT8_card_KOR.png
PSMT4_name.png      -> PSMT4_name_KOR.png
```

`graphics_extract/catalog.json`에는 원본 해시, 선택된 LEX, 추출 폴더와 결과
경로가 기록된다. 추출 후 원본 리소스가 달라졌다면 해당 파일을 다시
`extract`한 뒤 리빌드해야 한다.

최신 추출물의 팔레트 적용 편집 PNG는 실제 게임 index와 실제 RGB 팔레트를
가진 P 모드 PNG이며 별도 PNG alpha 채널을 사용하지 않는다. GraphicsGale에서
팔레트 index를 골라 칠하면 그 index가 그대로 XTX에 기록된다. RGBA로 변환하지
않고 indexed 상태로 저장한다.

기존 추출 폴더에 `_KOR.png`가 있으면 재추출 시 그 폴더를 덮어쓰지 않는다.
대신 `<이름>_indexed` 폴더에 새 형식으로 추출하고 카탈로그의 리빌드 대상을
새 폴더로 전환한다.

현재 `CardInfo1.bin`만 대응 LEX를 근거 있게 확정하지 못했으므로 색상 적용본
대신 indexed PSMT4/PSMT8 뷰로 추출한다. 나머지 115개는 동명 LEX 또는 확인된
공유 LEX 묶음을 자동 사용한다.
