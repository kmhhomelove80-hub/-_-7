# 키움 두 종목 순환매매

Windows 10/11 x64 데스크톱 자동매매 프로그램입니다. 이미 보유한 두 종목을 평균 매입가 기준으로 비교하여, 설정 비율 이상 하락한 종목에 상대 종목 매도금을 옮겨 추가 매수합니다.

## 실행파일 다운로드

[Releases에서 최신 Windows 실행파일 받기](https://github.com/kmhhomelove80-hub/-_-7/releases)

`KiwoomTwoStockTrader.exe`를 받아 실행하거나 `KiwoomTwoStockTrader-Windows-x64.zip`을 압축 해제합니다. EXE 배포본은 Python을 따로 설치하지 않아도 됩니다. `SHA256SUMS.txt`로 다운로드 파일의 해시를 확인할 수 있습니다.

**실전 REST API에 연결하는 프로그램입니다. 실제 계좌 인증·주문·체결은 개발 환경에서 검증하지 않았습니다.** 실행 직후에는 자동매매가 중지되어 있으며, 사용자 PC에서 키·계좌·종목·비율·일일 횟수를 입력한 뒤 시작합니다. 코드서명은 포함하지 않습니다.

## 주요 기능

- API 키·Secret·연결 계좌번호 최초 입력 후 Windows 자격 증명 저장소에 저장
- 두 종목의 평균 매입가 대비 하락률 비교, 더 많이 하락한 종목 우선 추가 매수
- 상대 종목 지정 비율 매도 → 전량 체결 확인 → 지정가 추가 매수
- 매수 후 새 평균 매입가로 조건 즉시 재평가
- 합산 매입원금 한도 50만원~10억원, 하루 최대 사이클 수 설정
- 주문번호·거래 기록 영속 저장, 불명확한 체결 상태에서는 재주문하지 않고 정지

[자세한 사용설명서](사용설명서.md)를 읽고 연결 확인/잔고 조회부터 진행하세요. **중지 버튼은 이미 접수된 주문을 취소하지 않습니다.**

## 소스 실행

Python 3.12 권장. Windows에서 `설치.bat`, `실행.bat` 순서로 실행합니다.

```powershell
python -m unittest discover -s tests -v
python -m pip install -r requirements-build.txt
./scripts/build_windows.ps1
```

GitHub Actions는 Windows에서 자동 테스트, 원본 UI 시작, EXE 패키지 UI 시작을 확인한 뒤 실행파일과 ZIP을 Release에 게시합니다. 키움 계좌에 연결하거나 실제 주문을 하는 빌드 테스트는 없습니다.

배포에 인증정보가 필요하지 않습니다. API 키, Secret, 계좌번호, 실계좌 로그를 저장소에 업로드하지 마세요.
