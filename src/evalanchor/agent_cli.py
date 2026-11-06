"""The goal-oriented OrderOps workflow: freeze, execute, replay, decide, review."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contracts import ContractError, fingerprint, read_json, write_json


def parser():
    p=argparse.ArgumentParser(prog='evalanchor arena',description=__doc__)
    s=p.add_subparsers(dest='command',required=True)
    build=s.add_parser('build',help='Write the versioned OrderOps benchmark and target matrix')
    build.add_argument('--output',type=Path,default=Path('examples/orderops'))
    oracle=s.add_parser('oracle',help='Verify environment reachability without model calls')
    oracle.add_argument('--benchmark',type=Path,default=Path('examples/orderops/benchmark.json'))
    oracle.add_argument('--output',type=Path,default=Path('artifacts/orderops-oracle.json'))
    run=s.add_parser('run',help='Plan by default; --execute authorizes the bounded paid run')
    run.add_argument('--benchmark',type=Path,default=Path('examples/orderops/benchmark.json'))
    run.add_argument('--configs',type=Path,default=Path('examples/orderops/configs.json'))
    run.add_argument('--output',type=Path,required=True)
    run.add_argument('--split',choices=['dev','test'],default='dev')
    run.add_argument('--case-id',action='append')
    run.add_argument('--config-id',action='append')
    run.add_argument('--repeats',type=int,default=1)
    run.add_argument('--workers',type=int,default=4)
    run.add_argument('--max-turns',type=int,default=8)
    run.add_argument('--schedule-seed',type=int,help='Shuffle complete case/repeat blocks before dispatch')
    run.add_argument('--budget',type=float,help='Explicit finite USD cap; required with --execute')
    run.add_argument('--campaign-id',help='Stable campaign identity; required with --execute')
    run.add_argument('--ledger',type=Path,default=Path('runs/orderops-budget.sqlite3'))
    run.add_argument('--env-file',type=Path,default=Path('.env.local'))
    run.add_argument('--execute',action='store_true')
    run.add_argument('--resume',action='store_true')
    audit=s.add_parser('audit',help='Replay original tool calls and recompute all grades')
    audit.add_argument('experiment',type=Path)
    analyze=s.add_parser('analyze',help='Produce finite-suite release decisions and paired diagnostics')
    analyze.add_argument('experiment',type=Path)
    analyze.add_argument('--output',type=Path,help='Separate analysis file; defaults to sibling analysis.json')
    upgrade=s.add_parser('assess',help='Audit a baseline/candidate upgrade under an explicit finite-suite policy')
    upgrade.add_argument('experiment',type=Path)
    upgrade.add_argument('--baseline',required=True)
    upgrade.add_argument('--candidate',required=True)
    upgrade.add_argument('--policy',type=Path)
    upgrade.add_argument('--output',required=True,type=Path)
    upgrade.add_argument('--markdown',type=Path)
    policy=s.add_parser('policy',help='Write the editable example upgrade policy; never a pre-registration claim')
    policy.add_argument('--output',required=True,type=Path)
    horizons=s.add_parser('compare-horizons',help='Strict paired comparison of complete 8/12-turn test matrices')
    horizons.add_argument('baseline',type=Path)
    horizons.add_argument('candidate',type=Path)
    horizons.add_argument('--output',type=Path,required=True)
    budget=s.add_parser('budget',help='Read the existing shared campaign ledger without model requests')
    budget.add_argument('--ledger',type=Path,default=Path('runs/orderops-budget.sqlite3'))
    budget.add_argument('--output',type=Path)
    serve=s.add_parser('serve',help='Local experiment and review workspace; never sends model requests')
    serve.add_argument('--workspace',type=Path,default=Path('experiments'))
    serve.add_argument('--port',type=int,default=8780)
    export=s.add_parser('export',help='Portable read-only experiment workbench')
    export.add_argument('--workspace',type=Path,default=Path('experiments'))
    export.add_argument('--output',type=Path,required=True)
    return p


def _separate_outputs(outputs, inputs):
    outputs=[p for p in outputs if p is not None]
    inputs=[p for p in inputs if p is not None]
    def aliases(left, right):
        return left.resolve()==right.resolve() or (left.exists() and right.exists() and left.samefile(right))
    for index, output in enumerate(outputs):
        if any(aliases(output, source) for source in inputs + outputs[:index]):
            raise ContractError('Output must not overwrite source evidence, policy, ledger, or another output')


def _preflight_output(args, benchmark, configs, cases):
    """Reject conflicting output/resume plans before credentials or client setup.

    The frozen runner still rechecks under its writer lock. This early read is
    not the concurrency guard; it prevents avoidable side effects on bad plans.
    """
    import sqlite3
    from .agent_runner import STRATEGIES, engine_fingerprint
    from .contracts import canonical
    output=args.output
    identity=output.name
    if not identity or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in identity):
        raise ContractError('Experiment ID must be a lowercase safe identifier')
    if output.exists() and not output.is_dir():
        raise ContractError('Experiment output must be a directory')
    journal=output/'journal.sqlite3'
    if journal.exists():
        if not args.resume:
            raise ContractError('Run journal exists; use --resume with the identical configuration')
        protocol={'repeats':args.repeats,'max_turns':args.max_turns,'split':args.split,'scoring_version':'orderops-state-v2'}
        if args.schedule_seed is not None:
            protocol['scheduling']={'kind':'seeded_case_repeat_blocks','seed':args.schedule_seed,'workers':args.workers}
        expected={'benchmark_hash':fingerprint(benchmark),'configs':configs,'prompts':STRATEGIES,'protocol':protocol,
                  'case_ids':[c['id'] for c in cases],'engine_hash':engine_fingerprint(),'id':identity}
        try:
            with sqlite3.connect(journal.resolve().as_uri()+'?mode=ro',uri=True) as db:
                row=db.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()
                if row is None or row[0]!=canonical(expected):
                    raise ContractError('Resume rejected: execution identity changed')
        except sqlite3.DatabaseError as exc:
            raise ContractError('Invalid existing run journal') from exc
    elif args.resume:
        raise ContractError('--resume requires an existing journal')
    elif output.exists() and any(output.iterdir()):
        raise ContractError('New experiment output contains existing files; use a fresh directory')


def main(argv=None):
    args=parser().parse_args(argv)
    from .orderops import benchmark_suite, scripted_oracle, World, evaluate_case
    from .agent_runner import default_configs, run_experiment, audit_experiment, validate_config
    if args.command=='build':
        benchmark=benchmark_suite()
        args.output.mkdir(parents=True,exist_ok=True)
        for name,value in [('benchmark.json',benchmark),('configs.json',default_configs())]:
            dest=args.output/name
            if dest.exists() and read_json(dest)!=value:
                raise ContractError('Refusing to replace a different frozen benchmark/config; choose a new output directory')
            write_json(dest,value)
        print(json.dumps({'cases':len(benchmark['cases']),'benchmark_hash':fingerprint(benchmark),'output':str(args.output)},indent=2))
    elif args.command=='oracle':
        benchmark=read_json(args.benchmark);results=[]
        for case in benchmark['cases']:
            world=World(case);trace=[];final=None
            for action in scripted_oracle(case):
                name=action['name'];arguments=action.get('arguments',action.get('args'))
                result=world.execute(name,arguments)
                trace.append({'name':name,'arguments':arguments,'args':arguments,'result':result})
                if result.get('done'):final=arguments;break
            grade=evaluate_case(case,world,trace,final)
            results.append({'case_id':case['id'],'grade':grade,'trace':trace,'final_state':world.snapshot()})
        receipt={'kind':'scripted_oracle_environment_validation_not_model_results','benchmark_hash':fingerprint(benchmark),'cases':len(results),'passed':sum(bool(r['grade']['task_success']) for r in results),'records':results}
        write_json(args.output,receipt)
        print(json.dumps({k:v for k,v in receipt.items() if k!='records'},indent=2))
        return 0 if receipt['passed']==receipt['cases'] else 1
    elif args.command=='run':
        benchmark=read_json(args.benchmark);configs=read_json(args.configs)
        if not 1 <= args.repeats <= 5 or not 1 <= args.workers <= 12 or not 2 <= args.max_turns <= 12:
            raise ContractError('Experiment repeats/workers/turn limits out of range')
        if args.schedule_seed is not None and not 0 <= args.schedule_seed < 2**32:
            raise ContractError('Schedule seed must be an integer in [0, 2**32)')
        for config in configs:validate_config(config)
        if len({c['id'] for c in configs})!=len(configs):raise ContractError('Duplicate config identity')
        if args.config_id:
            configs=[c for c in configs if c['id'] in args.config_id]
            if set(args.config_id)!={c['id'] for c in configs}:raise ContractError('Unknown config selection')
        cases=[c for c in benchmark['cases'] if c['split']==args.split and (not args.case_id or c['id'] in args.case_id)]
        if not cases or not configs:raise ContractError('Empty experiment selection')
        if args.case_id and set(args.case_id)!={c['id'] for c in cases}:raise ContractError('Unknown or wrong-split case selection')
        from .campaign import validate_campaign, open_campaign, validate_resume_campaign, bind_campaign
        if args.execute and (args.budget is None or args.campaign_id is None):
            raise ContractError('--execute requires explicit --budget and --campaign-id')
        cap = args.budget if args.budget is not None else 4.75
        campaign_id = args.campaign_id or 'unconfigured-plan'
        validate_campaign(campaign_id, cap)
        plan={'mode':'execute' if args.execute else 'plan','split':args.split,'cases':len(cases),'configs':len(configs),'repeats':args.repeats,'episodes':len(cases)*len(configs)*args.repeats,'maximum_network_requests':len(cases)*len(configs)*args.repeats*args.max_turns,'shared_ledger_cap_usd':cap,'campaign_id':campaign_id,'benchmark_hash':fingerprint(benchmark),'note':'Persistent reservation check before every request; unfinished cells are retained, not dropped. Shared ledger is required for the entire campaign.'}
        print(json.dumps(plan,indent=2),flush=True)
        if not args.execute:return 0
        _preflight_output(args, benchmark, configs, cases)
        if args.resume:
            validate_resume_campaign(args.output,args.ledger,campaign_id,cap)
        from .openai_provider import load_api_key
        from .agent_api import ResponsesClient
        ledger=open_campaign(args.ledger,campaign_id,cap)
        credential=load_api_key(args.env_file)
        bind_campaign(args.output,args.ledger,campaign_id,cap)
        client=ResponsesClient(api_key=credential,ledger=ledger,cache_dir=args.output/'calls')
        experiment=run_experiment(benchmark,configs,args.output,client=client,split=args.split,repeats=args.repeats,
            workers=args.workers,max_turns=args.max_turns,resume=args.resume,case_ids=args.case_id,schedule_seed=args.schedule_seed)
        # On exceptions the persistent ledger remains authoritative. Do not
        # overwrite a prior receipt for an experiment we failed to acquire.
        write_json(args.output/'budget-receipt.json',ledger.export())
        print(json.dumps({'status':experiment['status'],'episodes':len(experiment['records']),'budget':ledger.snapshot()},indent=2),flush=True)
    elif args.command=='audit':
        print(json.dumps(audit_experiment(read_json(args.experiment)),indent=2))
    elif args.command=='analyze':
        from .agent_analysis import summarize_experiment
        experiment=read_json(args.experiment)
        output=args.output or args.experiment.parent/'analysis.json'
        _separate_outputs([output], [args.experiment])
        write_json(output,summarize_experiment(experiment))
        print(json.dumps({'analysis':str(output),'source_unchanged':True},indent=2))
    elif args.command=='policy':
        from .upgrade import default_policy
        if args.output.exists():raise ContractError('Policy already exists; choose a new output path')
        write_json(args.output,default_policy())
        print(args.output)
    elif args.command=='assess':
        from .upgrade import assess_upgrade, render_upgrade_markdown
        _separate_outputs([args.output,args.markdown], [args.experiment,args.policy])
        report=assess_upgrade(read_json(args.experiment),args.baseline,args.candidate,read_json(args.policy) if args.policy else None)
        write_json(args.output,report)
        if args.markdown:
            args.markdown.parent.mkdir(parents=True,exist_ok=True)
            args.markdown.write_text(render_upgrade_markdown(report),encoding='utf-8')
        print(json.dumps({'decision':report['decision'],'report':str(args.output)},ensure_ascii=False,indent=2))
        return {'pass':0,'fail':1,'inconclusive':2}[report['decision']]
    elif args.command=='compare-horizons':
        from .agent_analysis import compare_horizons
        _separate_outputs([args.output], [args.baseline,args.candidate])
        result=compare_horizons(read_json(args.baseline),read_json(args.candidate))
        write_json(args.output,result)
        print(json.dumps({'comparison':str(args.output)},indent=2))
    elif args.command=='budget':
        from .campaign import ReadOnlyLedger
        _separate_outputs([args.output], [args.ledger])
        ledger=ReadOnlyLedger(args.ledger)
        if args.output:write_json(args.output,ledger.export())
        print(json.dumps(ledger.snapshot(),indent=2))
    elif args.command=='serve':
        from .workbench import serve_workbench
        serve_workbench(args.workspace,args.port)
    elif args.command=='export':
        from .workbench import export_dashboard
        if args.output.suffix.lower() != '.html':
            raise ContractError('Workbench export requires an .html output path')
        export_dashboard(args.workspace,args.output)
        print(args.output)
    return 0
