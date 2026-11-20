-- 010_reanudacion.sql — vocero-crm 073-A (D2, D3). Aditiva e idempotente.
--
-- dispatch_inbox.attempts / effects_at: un turno que Nea no terminó (redeploy,
-- OOM) se vuelve a correr si todavía no envió nada ni ejecutó herramientas
-- (`effects_at IS NULL`) y no lleva demasiados intentos; si ya tuvo efectos,
-- pasa a una persona como siempre. Ver app/efectos.py.
ALTER TABLE dispatch_inbox ADD COLUMN IF NOT EXISTS attempts int NOT NULL DEFAULT 0;
ALTER TABLE dispatch_inbox ADD COLUMN IF NOT EXISTS effects_at timestamptz;

-- pending_send.kind: además de respuestas (`message`), la cola durable lleva
-- los handoffs que el CRM no aceptó en el turno (`handoff`, con el motivo en
-- `content`).
ALTER TABLE pending_send ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'message';
