# fix.py
import os
from dotenv import load_dotenv
import psycopg2
load_dotenv()
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()
cur.execute("""
    UPDATE positions SET
        premium_received = 1.14,
        total_received   = 113.72,
        gross_pnl        = -40.28,
        net_pnl          = -40.28,
        pnl_pct          = -26.2,
        close_reason     = 'STOP_LOSS'
    WHERE id = 6
""")
conn.commit()
print('OK — P&L corregido: -$40.28')
cur.close()
conn.close()