@app.get("/api/activity")
async def activity():
    c=db()
    rows=c.execute("""SELECT 'version' type,COALESCE(v.updated_at,v.created_at) timestamp,r.full_name,v.version,v.status,v.error
      FROM versions v JOIN repos r ON r.id=v.repo_id
      UNION ALL SELECT 'file' type,COALESCE(f.downloaded_at,f.downloaded_at) timestamp,r.full_name,f.name,f.status,f.error
      FROM files f JOIN versions v ON v.id=f.version_id JOIN repos r ON r.id=v.repo_id
      WHERE f.downloaded_at IS NOT NULL ORDER BY timestamp DESC LIMIT 100""")
    rows=c.fetchall()
    c.close()
    return [rd(x) for x in rows] if rows else []
