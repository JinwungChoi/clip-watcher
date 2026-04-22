# clip-watcher

Windows 클립보드 이미지 감시기 + 트레이 아이콘. `Win+Shift+S` 등으로 클립보드에 이미지가 들어오면 자동으로 PNG로 저장하고, 변환된 경로(또는 R2 퍼블릭 URL)를 클립보드에 다시 복사합니다.

## 기능

- **자동 저장** — 클립보드 이미지를 `~/Pictures/ClipShots/clip_YYYYMMDD_HHMMSS.png`로 저장 (SHA1 해시로 중복 방지)
- **경로 모드 4종** — 트레이 메뉴에서 선택, 마지막 선택은 `.mode` 파일에 저장됨
  - `windows` — `C:\Users\...\clip_xxx.png`
  - `wsl` — `/mnt/c/Users/.../clip_xxx.png`
  - `forward` — `C:/Users/.../clip_xxx.png`
  - `r2` — Cloudflare R2에 업로드 후 퍼블릭 URL (`https://pub-xxx.r2.dev/clip_xxx.png`)
- **트레이 아이콘** — 일시정지/재개 토글, 저장 폴더/로그 열기, 모드 변경, 종료
- **로그** — `~/Pictures/ClipShots/watcher.log` (UTF-8, 콘솔 동시 출력 / pythonw 시 파일만)

## 설치

```bash
pip install -r requirements.txt
```

## 실행

```bash
python clip_watcher.py
# 콘솔 없이:
pythonw clip_watcher.py
```

`Ctrl+C` / 콘솔 X 버튼 / 트레이 메뉴 "종료" 모두로 깔끔하게 종료됨.

## R2 업로드 설정

스크립트 같은 폴더에 `.env` 파일 생성:

```env
R2_ACCESS_KEY=your-access-key
R2_SECRET_KEY=your-secret-key
R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
R2_BUCKET=your-bucket-name
R2_PUBLIC_URL=https://pub-xxxxx.r2.dev
```

설정이 누락되면 트레이 메뉴의 R2 항목이 자동으로 숨겨집니다. 환경변수로 넘겨도 됨.

## 시작프로그램 등록

`Win+R` → `shell:startup` → 다음 내용으로 `.bat` 또는 바로가기 생성:

```bat
pythonw "C:\path\to\clip_watcher.py"
```

## 요구사항

- Windows 10/11
- Python 3.12+
