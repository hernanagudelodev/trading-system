import os
from dotenv import load_dotenv
import psycopg2
load_dotenv()
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()
sql = "UPDATE positions SET status='CLOSED', closed_at=NOW(), premium_received=0.84, total_received=84, gross_pnl=-126, net_pnl=-126, pnl_pct=-60, close_reason='Stop loss oil drop' WHERE id=4"
cur.execute(sql)
conn.commit()
print('Rows updated:', cur.rowcount)
cur.close()
conn.close()