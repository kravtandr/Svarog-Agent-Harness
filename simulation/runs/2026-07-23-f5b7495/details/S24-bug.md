# S24: schedule approve happy path — НАЙДЕН БАГ (требует отдельной диагностики)
ПЕРВЫЙ прогон с ошибкой команды: `approve <id> "да"` упало ("unexpected extra argument") — approve берёт причину через --reason, не positional. Approval остался pending.
НО: resume ВСЁ РАВНО вернул completed с ответом "Ежедневная сводка успешно настроена" — при этом cron_jobs=0 (джоба НЕ создана).
Это потенциальный дефект: run completed с утверждением об успехе при непроведённом approval. Требует перепрогона с правильным approve.
