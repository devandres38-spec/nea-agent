-- 009 (vocero-crm 071, D1 y D7) — índices del reclamo por conversación y de
-- la purga. Idempotente: se re-aplica en cada arranque, como todas.

-- D1: el reclamo busca, por (organización, conversación), el despacho más
-- viejo sin terminar y si hay uno `started` con lease vivo.
CREATE INDEX IF NOT EXISTS dispatch_inbox_conv_idx
  ON dispatch_inbox (organization_id, conversation_id, created_at)
  WHERE state <> 'done';

-- D7: la purga borra por antigüedad los terminados (su `payload` lleva los
-- mensajes del cliente tal cual: no se guardan más de lo necesario) …
CREATE INDEX IF NOT EXISTS dispatch_inbox_done_idx
  ON dispatch_inbox (updated_at)
  WHERE state = 'done';

-- … y las marcas de deduplicación viejas.
CREATE INDEX IF NOT EXISTS idx_processed_message_at
  ON processed_message (processed_at);

-- … y los envíos diferidos ya cerrados (entregados o abandonados).
CREATE INDEX IF NOT EXISTS idx_pending_send_cerrado
  ON pending_send (created_at)
  WHERE delivered_at IS NOT NULL OR abandoned_at IS NOT NULL;
