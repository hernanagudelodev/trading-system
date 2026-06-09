import os
from dotenv import load_dotenv
import psycopg2
load_dotenv()
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()
cur.execute("""
    UPDATE paper_positions SET
        current_spread_value = NULL,
        current_value        = NULL,
        gross_pnl            = NULL,
        pnl_pct              = NULL,
        profit_pct_of_max    = NULL
    WHERE id = 8
""")
conn.commit()
print('OK')
cur.close()
conn.close()