#!/usr/bin/env python3
"""Plot only replay-audited, complete 8/12-turn OrderOps test matrices.

Matplotlib is an optional artifact-generation dependency, not a core runtime
dependency. This command never calls a model and refuses incomplete evidence.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from summarize_orderops_results import ROOT, load_campaign
from evalanchor.contracts import ContractError, write_json
from evalanchor.orderops import FAMILIES


def plot_inputs(data):
    if not data.get("formal_confirmation_complete") or data.get("kind") != "orderops_formal_results":
        raise ContractError("Plots require two complete audited formal test matrices")
    phases = {phase['id']:phase for phase in data['phases'] if phase['id'] in ('test-v2','test-long-v2')}
    if set(phases) != {'test-v2','test-long-v2'}:
        raise ContractError("Both test horizons are required")
    for phase in phases.values():
        if not all(phase['grid'][key] for key in ('experiment_finished','complete_selected_grid','complete_test_split','trusted_benchmark')):
            raise ContractError("Refusing a plot of partial, custom, or unverified test evidence")
    return phases


def plot_campaign(data, destination):
    phases = plot_inputs(data)  # Validate before importing plotting or creating files.
    cache = ROOT / '.cache' / 'matplotlib'
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('MPLCONFIGDIR', str(cache))
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.ticker import PercentFormatter, FuncFormatter
    except ImportError as exc:
        raise ContractError("Optional plotting dependency required: pip install 'matplotlib>=3.8,<4'") from exc
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.titlesize':12,'axes.labelsize':10,
                         'axes.spines.top':False,'axes.spines.right':False,'figure.facecolor':'white'})
    order = [row['config_id'] for row in phases['test-v2']['configs']]
    rows = {identity:{row['config_id']:row for row in phase['configs']} for identity,phase in phases.items()}
    labels = [identity.replace('luna','Luna').replace('terra','Terra').replace('-','\n') for identity in order]
    figure = plt.figure(figsize=(14,10),layout='constrained')
    grid = figure.add_gridspec(2,2,height_ratios=[1,1.25])
    quality, cost, heatmap = figure.add_subplot(grid[0,0]), figure.add_subplot(grid[0,1]), figure.add_subplot(grid[1,:])
    colors = ('#235a92','#d77725')
    for identity, offset, color, label in zip(('test-v2','test-long-v2'),(-.19,.19),colors,('8 turns (primary)','12 turns (ablation)')):
        x = [index+offset for index in range(len(order))]
        values = [rows[identity][config]['safe_success_rate'] for config in order]
        bars = quality.bar(x, values, width=.35, color=color, label=label, zorder=3)
        for bar,value in zip(bars,values):
            quality.annotate(f'{value*100:.1f}%',(bar.get_x()+bar.get_width()/2,value),xytext=(0,4),textcoords='offset points',ha='center',fontsize=9)
        amounts = [rows[identity][config]['cost']['total_usd'] for config in order]
        bars = cost.bar(x,[float('nan') if value is None else value for value in amounts],width=.35,color=color,label=label,zorder=3)
        for bar,value in zip(bars,amounts):
            if value is None:
                cost.text(bar.get_x()+bar.get_width()/2,.02,'Unknown',transform=cost.get_xaxis_transform(),ha='center',rotation=90,fontsize=8)
            else:
                cost.annotate(f'${value:.3f}',(bar.get_x()+bar.get_width()/2,value),xytext=(0,4),textcoords='offset points',ha='center',fontsize=9)
    quality.set(title='A. Safe task success',ylabel='Group-weighted safe success',ylim=(0,1.23),
                yticks=[0,.2,.4,.6,.8,1],xticks=range(len(order)),xticklabels=labels)
    quality.yaxis.set_major_formatter(PercentFormatter(1))
    quality.legend(loc='upper center',frameon=False,fontsize=9,ncol=2)
    quality.grid(axis='y',alpha=.2,zorder=0)
    cost.set(title='B. Estimated token cost per complete matrix slice',ylabel='USD per configuration',xticks=range(len(order)),xticklabels=labels)
    cost.set_ylim(bottom=0)
    cost.margins(y=.22)
    cost.yaxis.set_major_formatter(FuncFormatter(lambda value,_:f'${value:.2f}'))
    cost.grid(axis='y',alpha=.2,zorder=0)
    cells = []
    columns = []
    for identity,horizon in (('test-v2',8),('test-long-v2',12)):
        for config in order:
            columns.append(f'{horizon}T '+config.replace('luna','Luna').replace('terra','Terra').replace('-','\n'))
    for family in FAMILIES:
        values=[]
        for identity in ('test-v2','test-long-v2'):
            for config in order:
                by_family = {row['family']:row for row in rows[identity][config]['families']}
                values.append(by_family[family]['safe_success_rate'])
        cells.append(values)
    graphic = heatmap.imshow(cells,vmin=0,vmax=1,cmap='YlGnBu',aspect='auto')
    heatmap.set(title='C. Task-family safe success (paired variants and repeats stay within groups)',
                xticks=range(len(columns)),xticklabels=columns,yticks=range(len(FAMILIES)),
                yticklabels=[family.replace('_',' ') for family in FAMILIES])
    heatmap.axvline(3.5,color='white',linewidth=3)
    for row,values in enumerate(cells):
        for column,value in enumerate(values):
            heatmap.text(column,row,f'{value*100:.0f}%',ha='center',va='center',fontsize=10,color='white' if value>.6 else '#16222e')
    colorbar=figure.colorbar(graphic,ax=heatmap,fraction=.024,pad=.02)
    colorbar.ax.yaxis.set_major_formatter(PercentFormatter(1))
    figure.suptitle('OrderOps: frozen 8-turn / 12-turn comparison',fontsize=17,fontweight='bold')
    figure.supxlabel('Full authored test grids; no population guarantee. Costs are estimates, not invoices. Later runs have warmer input caches.',fontsize=9,color='#444444')
    destination=Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    figure.savefig(destination,dpi=180,bbox_inches='tight')
    figure.savefig(destination.with_suffix('.pdf'),bbox_inches='tight')
    plt.close(figure)
    write_json(destination.with_suffix('.metadata.json'),{
        'kind':'plot_from_complete_replay_audited_orderops_evidence','report_fingerprint':data['fingerprint'],
        'frozen_protocol_fingerprint':data['frozen_protocol_fingerprint'],
        'sources':{identity:{'sha256':phase['source_sha256'],'path':phase['restored_source_path'],
                              'observed_episodes':phase['grid']['observed_episodes']} for identity,phase in phases.items()},
        'population_inference':False,'matplotlib_version':matplotlib.__version__})
    return destination


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,default=ROOT/'experiments')
    parser.add_argument('--protocol',type=Path,default=ROOT/'artifacts/orderops-v2/frozen-protocol.json')
    parser.add_argument('--output',type=Path,default=ROOT/'docs/figures/orderops-results.png')
    args=parser.parse_args(argv)
    data=load_campaign(args.workspace,args.protocol,development_preview=False)
    path=plot_campaign(data,args.output)
    print(json.dumps({'figure':str(path),'pdf':str(path.with_suffix('.pdf')),'kind':'actual_complete_test_evidence'},indent=2))
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (ContractError,OSError,KeyError,TypeError) as exc:
        raise SystemExit(f'OrderOps plot withheld: {exc}') from None
