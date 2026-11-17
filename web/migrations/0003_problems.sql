-- Existing databases only: the kata -> problem rename. Run once (before npm run cards):
--   npm run db:migrate-problems
UPDATE OR IGNORE cards SET id = 'problem:' || substr(id, 6), topic = 'problems'
  WHERE id LIKE 'kata:%';
DELETE FROM cards WHERE id LIKE 'kata:%';
UPDATE OR IGNORE card_state SET card_id = 'problem:' || substr(card_id, 6)
  WHERE card_id LIKE 'kata:%';
DELETE FROM card_state WHERE card_id LIKE 'kata:%';
UPDATE attempts SET card_id = 'problem:' || substr(card_id, 6) WHERE card_id LIKE 'kata:%';
