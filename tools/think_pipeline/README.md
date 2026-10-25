# think 원본 기반 파이프라인

**2026-10-25 원문 초기화:** 사용자 요청으로 기존 think TXT 100개, BIN 100개와 `translations.json` 1,192개 항목을 `.bin.ori` 원문으로 되돌렸다. 현재 대응표도 원문 기준이다. 초기화 직전 한글 TXT·BIN·JSON·대응표·치환표는 `codex-lab/think-korean-backup-20261025`에 원래 경로 구조로 보존했고 203개 파일의 SHA-256을 검증했다. 아래 최초 이관 통계와 `codex-lab/think-build-current` 빌드는 초기화 이전 기록이다. 새로운 번역은 현재 JSON에서 시작하고 새 출력 폴더로 빌드한다.

기존 도구를 호출하지 않고 `thinkNNN.bin.ori`에서 구조를 해석해 BIN을 새로 만든다. 기존 도구·TXT·BIN은 그대로 보존했다. `.a` 적용은 기존 외부 파이프라인의 역할이며 여기서는 처리하지 않는다.

## 편집과 빌드

저장소 루트에서 실행한다. Python 표준 라이브러리만 필요하다.

```powershell
python -B tools/think_pipeline/pipeline.py build --check
python -B tools/think_pipeline/pipeline.py build --output codex-lab/think-build-next
```

`translations.json`의 `translation`만 편집한다. `source`, `offset`, `opcode`, 해시는 바꾸지 않는다. JSON의 `\n`은 개행, `\u0019` 같은 표기는 원본 제어문자다. 전각/반각 공백도 번역의 일부로 보존한다. `null`이면 원문을 유지하며, 빈 문자열은 공백으로 비운다. **기존 TXT의 변경은 자동 반영되지 않는다.** 최초 이관 후 이 JSON이 새 파이프라인의 번역 입력이다.

매번 `.bin.ori`에서 시작하므로 확장된 파일에 다시 확장을 누적하지 않는다. 출력 폴더는 새 경로여야 한다. 100개 `thinkNNN.bin`과 `build-report.json`을 출력하며, 결과 BIN을 기존 적용 파이프라인에 넘기면 된다. 현재 번역을 보존한 첫 빌드는 `codex-lab/think-build-current`에 있다. 현재 TXT가 원래 길이에 맞춰져 있어 이번 빌드의 확장 건수는 0이며, 길이 초과 지원은 별도의 긴 한글 테스트와 전체 문자열 확장 검사로 확인했다.

## 대응표

`translation_comparison.txt`는 UTF-8 BOM 텍스트다. 전체 1,192개 위치를 원문 569개로 묶고, 번역이 다른 경우 총 583개 변형을 구분했다. 기술명·대사·튜토리얼·상태 메시지·PRINT를 모두 포함한다.

- 같은 원문은 한 번만 표시하고 모든 사용 위치를 함께 적는다.
- 공백·개행·제어문자가 다른 원문은 별개로 둔다.
- 본문은 원문과 현재 번역 그대로이며 배치 확인 행에서 `[SP]`, `[FWSP]`, `[NL]`로 차이를 확인할 수 있다.
- 제어문자는 `\xNN`으로 표시한다. 대응표 자체는 패치 입력이 아니다.
- 현재 원문/번역의 공백·개행 배열이 다른 위치는 850개다. 정상적인 한국어 띄어쓰기 변경도 포함하므로 전부 복원 대상으로 판단하면 안 된다.

번역을 수정한 뒤 새 대응표를 만들려면:

```powershell
python -B tools/think_pipeline/pipeline.py compare --output codex-lab/think-comparison-next.txt
```

## 추출과 이관

```powershell
python -B tools/think_pipeline/pipeline.py extract --catalog codex-lab/think-translations-new.json
```

`--source`의 `.bin.ori` 구조와 같은 폴더의 기존 TXT를 오프셋으로 대조한다. 기존 번역 1,191개를 그대로 이관했다. 검증된 비문자열 오탐 5개는 `ignored_legacy_entries`에 기록하고 적용하지 않는다. 기존 TXT가 누락한 `PON`은 원문으로 추가했다. 인덱스 순서로 대응하지 않으므로 오탐 제거가 뒤쪽 번역을 밀지 않는다. 기존 카탈로그를 덮어쓰는 추출은 거부한다.

초기 백업: `codex-lab/think-pipeline-backup-20261025.zip`. 기존 도구·매핑·TXT·BIN·ORI·치환표 304개 파일과 SHA-256 목록이 들어 있으며 저장 후 내용 해시를 검증했다. 기존 생산 파일은 이번 작업에서 수정하지 않았다.

## 확장과 검증

짧은 번역은 기존 방식의 전각/반각 공백 패딩을 유지한다. 긴 번역은 기존 명령 시작의 4바이트를 GO로 바꾸고 파일 끝에 문자열 명령과 복귀 GO를 추가한다. 원래 코드/데이터 오프셋, 메시지 부가 피연산자는 유지한다. 문자열을 잘라내지 않으며 파일당 16,384바이트를 넘으면 출력 전에 실패한다. 잘못된 오프셋·중복 항목·원문 변경·내장 NUL도 거부한다.

```powershell
python -B -m unittest discover -s tools/think_pipeline -p test_pipeline.py -v
```

검증 범위: 번역 전부 이관, 원문 무변경 왕복, 1,192개 문자열의 홀수/짝수 확장 2,384건, 긴 한글 두 문장의 전체 빌드, 반복 빌드 동일성, 잘못된 입력 거부, 대응표 중복/변형 보존. 새 확장 함수를 원본 OV01의 MIPS 코드로 실행한 2,384건도 통과했다. 해당 CPU 검증은 기존 로컬 분석 환경을 사용했으며 일반 빌드 의존성은 아니다.

정적 루트에 연결되지 않은 문법상 문자열 후보도 포함하며 `rooted`로 구분한다. 전체 동적 데이터 흐름과 게임 화면 렌더링을 증명한 것은 아니다. 실제 게임에서 줄 수·폭·제어 코드·타이밍을 확인해야 하며, 16KB 제한은 화면 표시 가능 길이와 다르다. `0x1A PRINT`는 분석한 retail 경로에서 출력하지 않는다.
