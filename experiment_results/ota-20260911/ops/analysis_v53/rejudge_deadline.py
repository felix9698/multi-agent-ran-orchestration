import json,re,glob,os,sys,collections
DL=float(sys.argv[1]) if len(sys.argv)>1 else 60.0
os.chdir(os.path.expanduser('~/agentic_ran_coordinator_based_on_ORAN/experiment_results/ota-20260911'))
def targets(d):
    out={}
    def walk(o):
        if isinstance(o,dict):
            if 'targetId' in o and isinstance(o.get('requirements'),dict): out[o['targetId']]=o['requirements']
            for v in o.values(): walk(v)
        elif isinstance(o,list):
            for v in o: walk(v)
    walk(d.get('T')); return out
def rtts(board):
    f=f'{board}/sources/ue2/ue2-echo-client.jsonl'
    iss={};rep={}
    if not os.path.exists(f): return None
    for l in open(f,errors='ignore'):
        try: e=json.loads(l)
        except: continue
        if e.get('event')=='issued': iss[e['seq']]=e.get('issuedAtMs',e['atMs'])
        elif e.get('event')=='reply' and 'seq' in e: rep.setdefault(e['seq'],e['atMs'])
    return {s:(rep[s]-t if s in rep else None) for s,t in iss.items()}
rows=[]
for camp in ('blocks-v53','blocks-v53x','blocks-v53y'):
    for lf in sorted(glob.glob(f'ops/overnight/{camp}-b*-attempt*.log')):
        m=re.search(r'b(\d)s(\d)-(.+)-attempt(\d+)',lf); b,s,meth,att=m.groups()
        dd=re.findall(r'formal38guarded-[0-9T]+-[0-9a-f]{32}',open(lf,errors='ignore').read())
        if not dd: continue
        eps=glob.glob(dd[-1]+'/evidence/AGENT-*-episode.json')
        if not eps or not os.path.exists(dd[-1]+'/live-sitting.stdout') or not re.search(r'^termination',open(dd[-1]+'/live-sitting.stdout',errors='ignore').read(),re.M): continue
        d=json.load(open(eps[0])); th=targets(d); R=rtts(dd[-1])
        old_any=new_any=False; newcols=oldcols=0; per_trial=[]
        for t in d.get('trials',[]):
            v=t.get('verdicts') or {}
            c=((t.get('window') or {}).get('cohorts') or {}).get('deadlineSuccessRatio@ue2') or {}
            co=c.get('cohort') or {}
            r=None
            if R is not None and co.get('firstSeq') is not None and co.get('issued'):
                seqs=range(co['firstSeq'],co['lastSeq']+1)
                r=sum(1 for q in seqs if R.get(q) is not None and R[q]<=DL)/co['issued']
            per_trial.append(r)
            for T,vv in v.items():
                others=all(x=='PASS' for k,x in vv.items() if k!='I2d.r1')
                old=all(x=='PASS' for x in vv.values())
                thr=(th.get(T) or {}).get('I2d.r1')
                new= others and (r is not None and thr is not None and r>=thr) if 'I2d.r1' in vv else old
                oldcols+=old; newcols+=bool(new)
                if old and t.get('counted',True): old_any=True
                if new and t.get('counted',True): new_any=True
        rows.append((camp,att,meth,old_any,new_any,newcols,[None if x is None else round(x,2) for x in per_trial]))
agg=collections.defaultdict(lambda: collections.Counter())
for camp,att,meth,o,n,nc,pt in rows:
    print(camp,att,meth,'old',o,'new',n,'newCols',nc,'ratio60',pt)
    g=agg[meth]; g['boards']+=1; g['old']+=o; g['new']+=n
print('\n== deadline',DL,'ms ==')
for m,g in agg.items(): print(m,dict(g))
