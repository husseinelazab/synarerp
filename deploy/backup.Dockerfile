# حاوية النسخ الاحتياطي: أدوات PostgreSQL 16 + GnuPG لتشفير النسخ + rclone لرفعها خارج الخادم
FROM postgres:16-alpine
RUN apk add --no-cache gnupg rclone
