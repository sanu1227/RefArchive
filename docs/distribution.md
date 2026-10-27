# RefArchive 배포 안내

이 안내는 0.7.0용입니다. 아래 고정 다운로드 링크는 공개된 최신 설치파일을 제공합니다. 받은 파일의 버전은 릴리스 페이지와 앱의 실행 상태에서 확인하세요. 이미 내려받은 파일이 자동으로 바뀌지는 않습니다.

## 전달할 파일

권장 방식은 [최신 설치파일 다운로드](https://github.com/sanu1227/RefArchive-releases/releases/latest/download/RefArchive-Setup-x64.exe) 고정 링크를 전달하는 것입니다. [ZIP 다운로드](https://github.com/sanu1227/RefArchive-releases/releases/latest/download/RefArchive-Portable-x64.zip), [최신 변경 내용](https://github.com/sanu1227/RefArchive-releases/releases/latest), [다운로드 안내 페이지](https://github.com/sanu1227/RefArchive-releases)도 제공합니다. 이 저장소는 공개이며 받는 사람은 GitHub 로그인 없이 다운로드할 수 있습니다.

- 직접 전달 권장: `RefArchive-0.7.0-Setup-x64.exe`. Windows 10/11 64비트용이며 관리자 권한과 Python 설치가 필요 없습니다. 설치 화면은 한국어를 지원합니다.
- 압축형: `RefArchive-0.7.0-Portable-x64.zip`. 전체 압축을 풀고 `RefArchive.exe`를 실행합니다. 실행파일만 떼어 보내지 마세요.
- 무결성 확인: 같은 배포 폴더의 `SHA256SUMS.txt`. PowerShell의 `Get-FileHash -Algorithm SHA256 파일경로`로 비교합니다.

배포본은 개인 자료·백업·테스트 이미지·모델을 포함하지 않습니다. 사용자는 빈 자료함에서 시작합니다. 자료를 함께 전달하려면 필요한 자료만 별도 ZIP 백업으로 전달하세요. 프로그램 배포파일과 개인 백업을 혼동하지 마세요.

## 안내를 읽는 방법

설치형 업데이트는 새 **RefArchive-Setup-x64.exe**를 다운로드한 뒤 기존 앱의 **보관 · 백업 → 프로그램 종료**를 누르고 설치파일을 실행하세요. 기존 설치를 먼저 제거할 필요가 없습니다. 같은 PC·Windows 계정에서 기존 자료를 이어서 사용합니다. 처음 설치형으로 바꾸는 압축형 사용자도 같은 기본 자료 위치를 사용하므로 재업로드하지 않아도 됩니다. 사용자 지정 데이터 경로나 개발용 소스 자료함은 별도로 ZIP 백업·복원하세요.

ZIP 업데이트는 새 폴더에 압축을 푸세요. 0.4.0 이상 사용자는 앱 **보관 · 백업 → 받은 버전 정보 확인**에서 새 폴더의 **업데이트 정보.json**으로 버전을 비교할 수 있습니다. 처음 0.4.0으로 업데이트할 때는 파일 이름과 이 안내를 참고하세요. 현재 자료함을 ZIP 백업한 뒤 앱에서 종료하고 새 EXE를 실행합니다. 같은 PC·Windows 계정의 배포본 자료는 유지됩니다. 자동 원격 알림이나 자동 설치는 제공하지 않습니다.

## 다음 버전을 고정 링크에 올리는 방법

1. 새 버전 번호로 배포본을 빌드하고 실행·자료 보존을 검증합니다. 이미 공개한 버전은 그대로 보관하고 버전 번호를 올리세요.
2. 빌드 출력 폴더 안의 **온라인 배포** 폴더를 엽니다. 빌드할 때 최신 설치파일, ZIP, 버전 정보, 체크섬 파일을 이 폴더에 준비합니다.
3. 본인의 GitHub 계정으로 [새 릴리스 작성](https://github.com/sanu1227/RefArchive-releases/releases/new)을 엽니다.
4. 새 태그를 만듭니다. 예를 들어 버전 0.4.1이면 `v0.4.1`, 제목은 `RefArchive 0.4.1`로 작성합니다. 변경 내용도 적습니다.
5. **온라인 배포** 폴더의 `RefArchive-Setup-x64.exe`, `RefArchive-Portable-x64.zip`, `update-info.json`, `SHA256SUMS.txt` 네 파일을 첨부합니다. 설치파일과 ZIP 이름에 버전 번호를 붙이지 마세요. 각 고정 다운로드 주소가 이 이름을 사용합니다. ZIP도 함께 올려야 기존 ZIP 링크가 유지됩니다.
6. 일반 배포라면 시험판(Pre-release)을 선택하지 않고 최신 릴리스(Set as the latest release)로 지정한 뒤 **Publish release**를 누릅니다. 초안(Draft)으로만 저장하면 받는 사람에게 공개되지 않습니다.
7. 위의 **최신 설치파일 다운로드** 링크에서 새 파일이 내려받아지는지 확인합니다. 설치 후 앱의 **보관 · 백업 → 실행 상태와 업데이트**에서 버전도 확인합니다.

받는 사람에게 같은 설치형 고정 링크를 다시 안내하면 됩니다. 이미 다운받은 파일이 자동으로 바뀌지는 않습니다. 받는 사람은 새 설치파일을 다운로드하고 기존 앱을 종료한 뒤 설치파일을 실행합니다. 공유 대상은 프로그램 파일이며 개인 자료함 백업은 이 공개 저장소에 올리지 마세요.

받는 사람이 처음에는 시험 자료로 **다른 PC 배포 점검표**를 확인하도록 안내하세요. 문제가 생기면 **진단 보고서 저장**으로 결과를 저장해 직접 전달할 수 있습니다. 보고서는 자료 내용·경로·로그 원문을 제외하며 자동 전송하지 않습니다. 실제 다른 PC에서 시험하기 전에는 해당 환경에서 검증됐다고 안내하지 마세요.

MD 파일을 열거나 별도 프로그램을 설치할 필요가 없습니다. 앱 상단의 **사용법** 버튼을 누르면 목차가 있는 사용설명서가 브라우저에 열립니다. 설치 후 시작 메뉴의 **RefArchive 사용설명서**, **RefArchive 배포 안내**에서도 열 수 있습니다.

압축 해제형은 폴더의 **사용설명서.html**, **배포안내.html**을 더블클릭하세요. 프로그램이 꺼져 있어도 인터넷 없이 읽을 수 있습니다. HTML 파일과 함께 있는 `guide.css`는 안내의 모양을 정하는 파일이므로 폴더에 그대로 두세요. 간단한 첫 실행 순서는 **처음 읽어주세요.txt**에서도 확인할 수 있습니다.

## 받는 사람의 사용

설치 후 바탕화면의 RefArchive를 클릭합니다. 브라우저가 자동으로 열립니다. 자료 저장·검색은 오프라인으로 사용할 수 있고, 자동 분류 모델 최초 준비와 외부 링크 기능에만 인터넷이 필요합니다. OCR는 Windows에 설치된 한국어·영어 OCR 언어팩을 사용합니다.

기본 포트는 18765이며 사용 중이면 18766~18784 중 빈 포트를 사용합니다. 바로가기를 다시 실행하면 같은 자료함의 화면을 엽니다. 브라우저 탭을 닫아도 서버는 유지됩니다. 화면의 **보관 · 백업 → 프로그램 종료**로 종료합니다.

설치형과 압축 해제형은 같은 계정의 `%LOCALAPPDATA%\RefArchive\data`를 사용합니다. 모델은 `%LOCALAPPDATA%\RefArchive\models`에 별도 저장됩니다. 설치 폴더 밖의 자료·모델은 업데이트와 제거 후에도 유지됩니다. 업데이트·제거 전에는 프로그램을 정상 종료하세요. 분석 중인 앱을 강제로 종료하지 않습니다.

기존 소스 버전의 자료를 옮기려면 기존 앱에서 ZIP 백업 후 배포본에서 복원합니다. 자동 이동하거나 기존 자료를 덮어쓰지 않습니다. 복원은 현재 자료함을 교체하므로 필요한 자료는 먼저 백업합니다.

## 문서와 자료 날짜

0.5.0은 DOCX·XLSX·PPTX·TXT 등 문서 원본 보관과 텍스트 미리보기·검색, 별도 자료 날짜 편집·정렬·연월 그룹·기간 필터를 제공합니다. 기존 자료의 날짜는 미지정으로 유지합니다. 스키마 v5로 변환한 자료함·백업은 구형 앱에서 열 수 없으므로 업데이트 전 ZIP 백업을 보관하세요. 문서 형식과 연결 앱 열기의 제한은 사용설명서를 참고하세요.

## 이전 기능과 호환

MP4·WebM 동영상, 묶음 편집, 접을 수 있는 분석 표시와 갤러리 화면을 지원합니다. 기존 자료함은 필요한 경우 처음 열 때 새 형식으로 변환하고 변환 전 DB 사본을 보관합니다. 기존 원본·키워드·메모·자료 모음은 유지됩니다. 묶음 대표 파일과 대용량 백업을 사용하려면 0.4.0 이상으로 여세요. 이전 버전으로 돌아가려면 업데이트 전 ZIP 백업을 사용하세요.

## 다시 빌드하기

Windows x64 Python 환경과 [Inno Setup 6.7.3](https://jrsoftware.org/isdl.php)을 준비합니다. 검증한 Python은 3.14.7입니다. 다음 명령은 프로젝트 루트에서 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe packaging\build_release.py --compiler "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" --output-dir release\0.7.0
.\.venv\Scripts\python.exe packaging\smoke_release.py --bundle release\0.7.0\RefArchive
```

위 명령은 기존 배포본과 별도로 `release/0.7.0`에 실행파일·설치파일·ZIP·온라인 배포용 사본을 만듭니다. `--output-dir`을 생략하면 기존처럼 `release`에 생성합니다. 설치·제거 시험은 기존 설치와 실행 중 앱을 바꿀 수 있으므로 별도의 시험 환경에서 진행하세요.

빌드 스크립트는 필요한 웹 화면·실행 스크립트·사용설명서만 지정해 포함합니다. 개인 자료 폴더를 수집하지 않습니다. 설치파일·ZIP·파일별 체크섬·라이브러리 버전·라이선스 고지를 생성합니다. `requirements-release.txt`는 실행 의존성 버전을 고정하며 `requirements-build.txt`는 빌드 도구를 추가합니다. 빌드 출력과 도구는 Git에 포함하지 않습니다.

`packaging/render_guides.py`는 개발용 MD 원본을 목차가 있는 HTML로 변환합니다. Markdown은 빌드 도구이며 앱 실행에는 필요 없습니다. 배포 안내 HTML에는 받는 사람에게 필요한 내용만 포함하고 재빌드 명령은 제외합니다. 문서를 수정하면 `python packaging/render_guides.py` 또는 배포 빌드를 실행해 HTML을 갱신합니다. 배포본의 사용자 안내에는 MD 원본을 넣지 않습니다.

PDF 처리는 [pypdfium2/PDFium](https://github.com/pypdfium2-team/pypdfium2)으로 제공합니다. PyMuPDF는 배포본에서 제외합니다. flatbuffers wheel에 없는 라이선스 원문은 [공식 v25.12.19 LICENSE](https://github.com/google/flatbuffers/blob/v25.12.19/LICENSE)를 `packaging/licenses`에 보관합니다. 배포본의 `THIRD_PARTY_NOTICES.txt`에 실행 의존성·Python·PDFium의 고지를 포함합니다.

실행파일 시험은 임시 사용자 경로와 Python이 없는 PATH를 사용합니다. OCR 시험은 설치된 한국어 OCR 언어팩을 사용하고, 실제 ONNX 시험은 개발용 모델이 있으면 임시 프로필에만 복사합니다. 설치 시험은 기존 RefArchive 설치가 없을 때만 별도 시험 경로에 설치하며, 개인 자료의 체크섬을 비교한 뒤 시험 설치를 제거합니다. 실제 다른 PC의 깨끗한 Windows 설치 환경 시험과는 구분합니다.

현재 배포파일은 코드 서명하지 않았습니다. 다른 PC에서 [SmartScreen 다운로드 경고](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation)가 나타날 수 있습니다. 조직 밖의 대규모 공개 배포에는 게시자 코드 서명과 실제 다른 PC 검증을 추가하세요. macOS·Linux·Windows ARM64용 배포본은 제공하지 않습니다.

## 0.3.0 묶음 자료 업데이트

여러 파일을 카드 하나로 저장하는 묶음 자료를 추가했습니다. 기존 폴더형 기능은 모음으로 표시합니다. 기존 자료와 모음은 보존하고 묶음을 자동 생성하지 않습니다. 새 DB는 버전 4이며 버전 1·2·3에서 처음 실행할 때 변환 전 DB 사본을 보관합니다. 0.3.0에서 만든 자료함·백업은 0.3.0 이상으로 여세요.

받는 사람은 기존 프로그램을 종료한 뒤 새 설치파일을 실행하거나, 새 ZIP 전체를 풀고 실행하면 됩니다. 같은 PC·Windows 계정의 기존 자료를 자동으로 이어서 사용하며 재업로드나 백업 복원은 필요 없습니다. 업데이트 전 ZIP 백업은 별도로 보관할 수 있습니다.
