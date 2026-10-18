import { PGlite } from '@electric-sql/pglite';
import readline from 'node:readline';
process.env.TZ='UTC'; // Match psycopg's naive UTC timestamp semantics.
const db=new PGlite({parsers:{1114:value=>value},serializers:{1114:value=>value}});
const reader=readline.createInterface({input:process.stdin});
for await (const line of reader) {
  try {
    const req=JSON.parse(line);
    if(req.exec) { await db.exec(req.exec); console.log(JSON.stringify({rows:[],fields:[]})); continue; }
    let i=0;
    const sql=req.sql.replace(/%s/g,()=>'$'+(++i));
    const result=await db.query(sql,req.args||[]);
    console.log(JSON.stringify(result));
  } catch(e) { console.log(JSON.stringify({error:e.message})); }
}
await db.close();
