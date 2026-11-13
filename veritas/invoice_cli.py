"""Command line entry point for the special-audit invoice workflow."""
import argparse
import json
from pathlib import Path

from veritas.invoicing import DemoAdapter, InvoiceWorkflow
from veritas.wip import load_wip
from veritas.invoice_lock import exclusive


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['demo', 'assisted', 'live'], default='live')
    parser.add_argument('--actor', default='', help='Named operator for auditable execution')
    parser.add_argument('--root', type=Path, default=Path('outputs/invoicing'))
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('wip', type=Path)
    create.add_argument('--month', required=True, help='YYYY-MM')
    create.add_argument('--country', default='Dubai')
    commands.add_parser('list')
    for name in ('show', 'advance', 'approve'):
        sub = commands.add_parser(name)
        sub.add_argument('run_id')
        if name == 'approve':
            sub.add_argument('--reviewer', required=True)
            sub.add_argument('--reviewer-role', default='reviewer')
    reconcile = commands.add_parser('reconcile')
    reconcile.add_argument('run_id')
    reconcile.add_argument('item_id')
    reconcile.add_argument('response', type=Path, help='Verified gateway response JSON file')
    reconcile.add_argument('--reviewer', required=True)
    args = parser.parse_args(argv)
    if args.mode == 'demo':
        adapter = DemoAdapter(args.root / 'demo' / 'artifacts')
    elif args.mode == 'assisted':
        from veritas.assisted import AssistedAdapter
        adapter = AssistedAdapter(args.root / 'assisted')
    else:
        from veritas.invoice_gateway import configured_adapter
        adapter = configured_adapter()
    if args.mode != 'demo' and args.command not in ('show', 'list', 'approve') and not args.actor.strip():
        parser.error('--actor is required for non-demo workflow operations')
    workflow = InvoiceWorkflow(args.root / args.mode, adapter, actor=args.actor.strip() or 'demo operator')
    try:
        with exclusive(args.root / args.mode):
            result = execute_command(workflow, args)
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        parser.exit(1, f'{exc}\n')
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def execute_command(workflow, args):
    if args.command == 'create':
        return workflow.create(load_wip(args.wip), args.month, args.country)
    if args.command == 'list':
        return workflow.list_runs()
    if args.command == 'show':
        return workflow.get(args.run_id)
    if args.command == 'approve':
        return workflow.approve(args.run_id, args.reviewer, args.reviewer_role)
    if args.command == 'reconcile':
        return workflow.reconcile(args.run_id, args.item_id,
                                  json.loads(args.response.read_text(encoding='utf-8')), args.reviewer)
    return workflow.advance(args.run_id)


if __name__ == '__main__':
    main()
