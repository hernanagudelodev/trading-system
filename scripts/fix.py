import os
from dotenv import load_dotenv
import psycopg2
load_dotenv()
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()
cur.execute("DELETE FROM paper_positions WHERE id = 7")
conn.commit()
print('OK — registro eliminado')
cur.close()
conn.close()