# 📊 پنل شخصی تحلیل بورس ایران

نسخه اولیه قابل اجرا با FastAPI و رابط فارسی RTL.

این پروژه از داده Demo استفاده نمی‌کند. Provider پیش‌فرض از endpointهای زنده TSETMC استفاده می‌کند. این endpointها جامعه‌محور/غیررسمی‌اند و ممکن است تغییر کنند یا از IP خارج ایران مسدود شوند. برای استفاده پایدارتر، Provider رسمی نیز به‌صورت جداگانه در معماری پیش‌بینی شده است.

## اجرا
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Windows:
`.venv\Scripts\activate`

سپس `http://localhost:8000` را باز کنید.

## تنظیم
در `.env`:
```env
DATA_PROVIDER=tsetmc_public
APP_USERNAME=admin
APP_PASSWORD=change-me-now
SESSION_SECRET=یک-رشته-تصادفی-طولانی
```

## API
- `POST /api/analyze`
- `POST /api/refresh`
- `GET /api/stocks/search?q=`
- `GET /api/watchlist`
- `POST /api/watchlist/{symbol}`
- `DELETE /api/watchlist/{symbol}`
- `GET /api/history`
- `GET /health`

کلیدها و Secretها در Backend می‌مانند.


## استقرار آنلاین با Render
این پروژه فایل `render.yaml` دارد و برای Deploy به‌صورت Web Service روی Render آماده شده است. پس از اتصال مخزن GitHub به Render، سرویس با FastAPI و پورت `$PORT` اجرا می‌شود.

نکته: در پلن رایگان، فایل‌سیستم سرویس پایدار نیست؛ بنابراین برای نگهداری دائمی تاریخچه و واچ‌لیست بهتر است بعداً PostgreSQL یا Persistent Disk اضافه شود.
