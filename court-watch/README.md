# court-watch — 테니스장 빈자리 텔레그램 알림 (개인용)

공개 예약 페이지를 주기적으로 조회해, **새로 빈 슬롯**이 생기면 텔레그램으로 알려줍니다.
로그인·예약·결제는 하지 않습니다. 알림의 링크를 눌러 직접 예약하세요.

## 감시 대상 (config.json)
- 고양특례시테니스협회: 성사실외(`id 5`), 성사전천후(`id 4`)
- 은평구시설관리공단: 북한산 장미 1·2면, 선정 1·2·3면
- 필터: 평일은 18시 이후 슬롯만, 주말·`extra_holidays`는 전 시간대
- 조회 범위: 오늘부터 `horizon_days`(기본 14일)

## 설정 순서 (최초 1회, PC 권장)
1. **텔레그램 봇 만들기**: 텔레그램에서 `@BotFather` → `/newbot` → 안내대로 이름 입력 → **토큰** 복사
2. **chat id 확인**: 만든 봇에게 아무 메시지나 보낸 뒤, 브라우저에서
   `https://api.telegram.org/bot<토큰>/getUpdates` 를 열어 `"chat":{"id":숫자}` 의 숫자를 복사
   (토큰은 비밀번호와 같으니 남에게 보여주지 마세요)
3. **GitHub 저장소 만들기**: 새 저장소(**Public 권장**, 아래 참고)를 만들고 이 폴더의 파일을 모두 업로드
   (`.github/workflows/court-watch.yml` 경로가 그대로 유지되어야 합니다)
4. **비밀키 등록**: 저장소 Settings → Secrets and variables → Actions → New repository secret
   - `TELEGRAM_BOT_TOKEN` = 1번 토큰
   - `TELEGRAM_CHAT_ID` = 2번 숫자
5. **연결 테스트**: Actions 탭 → court-watch → Run workflow → `test_telegram` 체크 후 실행 → 텔레그램에 메시지 도착 확인
6. **첫 실행**: Run workflow(체크 해제)로 한 번 실행하면 "감시 시작" 메시지가 오고, 이후 10분마다 자동 실행됩니다.

## 알아둘 점
- **Public vs Private**: Private 저장소는 무료 실행 시간이 월 2,000분이라 10분 간격 상시 실행 시 초과할 수 있습니다.
  Public은 무료 한도 걱정이 없지만 config.json·state.json(코트 이름, 빈 슬롯 목록)이 공개됩니다. 개인정보와 비밀키는 공개되지 않습니다.
- GitHub 정기 실행은 **최소 5분 간격이며 5~30분 지연될 수 있습니다.** 인기 코트 취소분은 알림이 와도 이미 잡혔을 수 있습니다.
- 60일간 저장소 활동이 없으면 정기 실행이 꺼질 수 있습니다. 슬롯이 바뀌면 state.json이 갱신되어 보통 유지됩니다.
- 사이트 화면 구조가 바뀌면 조회가 실패합니다. 6회 연속 실패하면 텔레그램으로 경고합니다.
- 클라우드(GitHub) IP를 사이트가 차단하면 조회가 실패할 수 있습니다. 실제로 돌려봐야 알 수 있습니다.
- 각 사이트 약관의 자동 조회 관련 조항은 확인하지 못했습니다. 조회 빈도를 늘리지 마세요.

## 코트·조건 바꾸기
`config.json`만 수정합니다.
- 고양: 코트 페이지 주소의 숫자가 `id` (`https://www.gytennis.or.kr/daily/5` → 5)
- 은평: 대관신청 화면에서 시설·장소를 고른 뒤 주소의 `part`, `place` 값을 사용
- `weekday_night_start_hour`: 평일 야간 시작 시각(기본 18)

## 로컬 시험
```
pip install -r requirements.txt
python -m pytest -q                 # 오프라인 테스트
python monitor.py --dry-run         # 실제 조회 후 메시지만 출력 (발송·저장 안 함)
```
