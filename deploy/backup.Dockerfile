# حاوية النسخ الاحتياطي: أدوات PostgreSQL 16 + GnuPG لتشفير النسخ
FROM postgres:16-alpine
RUN apk add --no-cache gnupg
