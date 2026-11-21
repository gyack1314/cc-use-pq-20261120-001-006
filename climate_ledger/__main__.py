import argparse
import json
import sys
from pathlib import Path

from .application import Application
from .contracts import DomainError, canonical
from .publication import Publication


def load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DomainError("input", "无法读取JSON输入", path=str(path)) from exc


def parser():
    root = argparse.ArgumentParser(description="凉城证据簿：城市气候适应资料与分析")
    root.add_argument("--database", default="climate.sqlite3", help="本地证据账文件")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect", help="检查资料与存储完整性")
    commands.add_parser("audit", help="复核历次入账与发布证据链")
    backup = commands.add_parser("backup", help="生成一致的证据账备份")
    backup.add_argument("destination")
    ingest = commands.add_parser("ingest", help="原子导入资料包")
    ingest.add_argument("input")
    for name in ("analyze", "compare", "diagnose", "plan", "equity", "priorities"):
        command = commands.add_parser(name, help="读取分析条件并生成结果")
        command.add_argument("input")
        if name != "diagnose":
            command.add_argument("--seq", type=int)
    history = commands.add_parser("history", help="查看对象修订来源")
    history.add_argument("kind")
    history.add_argument("identity")
    draft = commands.add_parser("draft", help="生成带来源证据的分析草稿")
    draft.add_argument("input")
    draft.add_argument("--id", required=True)
    draft.add_argument("--scope", required=True)
    draft.add_argument("--author", required=True)
    draft.add_argument("--kind", choices=("exposure", "study"), default="exposure")
    review = commands.add_parser("review", help="审核特定摘要的分析草稿")
    review.add_argument("id")
    review.add_argument("digest")
    review.add_argument("reviewer")
    publish = commands.add_parser("publish", help="按专题前序签发记录")
    publish.add_argument("draft")
    publish.add_argument("release")
    publish.add_argument("--expected-head")
    export = commands.add_parser("export", help="导出不可变发布文件")
    export.add_argument("release")
    export.add_argument("destination")
    verify = commands.add_parser("verify", help="校验导出的发布文件")
    verify.add_argument("input")
    reconcile = commands.add_parser("reconcile", help="核对两次资料入账的分析差异")
    reconcile.add_argument("input")
    reconcile.add_argument("before", type=int)
    reconcile.add_argument("after", type=int)
    csv_import = commands.add_parser("ingest-csv", help="导入一个类型的CSV资料")
    csv_import.add_argument("kind")
    csv_import.add_argument("input")
    csv_import.add_argument("--id", required=True)
    csv_import.add_argument("--source", required=True)
    csv_export = commands.add_parser("export-csv", help="导出当前资料快照")
    csv_export.add_argument("directory")
    csv_export.add_argument("--seq", type=int)
    preview = commands.add_parser("decision-preview", help="按当时预报预览适应组合（不占名额）")
    preview.add_argument("input")
    confirm = commands.add_parser("decision-confirm", help="确认组合并占用预算与设施名额")
    confirm.add_argument("input")
    confirm.add_argument("--select", required=True, help="逗号分隔的候选行动编号")
    cancel = commands.add_parser("decision-cancel", help="取消预留并按截止时刻结算退费")
    cancel.add_argument("decision")
    cancel.add_argument("--actions", required=True)
    cancel.add_argument("--at", required=True)
    activate = commands.add_parser("decision-activate", help="启用已预留行动并结算启用费")
    activate.add_argument("decision")
    activate.add_argument("--actions", required=True)
    activate.add_argument("--at", required=True)
    adjust = commands.add_parser("decision-adjust", help="显式确认调整：取消旧预留后确认新组合")
    adjust.add_argument("decision")
    adjust.add_argument("input")
    adjust.add_argument("--select", required=True)
    adjust.add_argument("--at", required=True)
    show = commands.add_parser("decision-show", help="查看决定事件链与费用台账")
    show.add_argument("decision")
    commands.add_parser("decision-list", help="列出全部决定")
    verify_decision = commands.add_parser("decision-verify", help="以有效观测事后核验并归因")
    verify_decision.add_argument("decision")
    verify_decision.add_argument("--minimum-coverage")
    return root


def dispatch(app, args):
    if args.command == "inspect":
        return app.inspect()
    if args.command == "audit":
        return app.audit()
    if args.command == "backup":
        return app.backup(args.destination)
    if args.command == "ingest":
        return app.ingest(load(args.input))
    if args.command in ("analyze", "compare", "plan", "equity", "priorities"):
        return getattr(app, args.command)(load(args.input), seq=args.seq)
    if args.command == "diagnose":
        return app.diagnose(load(args.input))
    if args.command == "history":
        return app.history(args.kind, args.identity)
    if args.command == "draft":
        return app.draft(args.id, args.scope, load(args.input), args.author, kind=args.kind)
    if args.command == "review":
        return app.review(args.id, args.digest, args.reviewer)
    if args.command == "publish":
        return app.publish(args.draft, args.release, args.expected_head)
    if args.command == "export":
        return app.publication.export(args.release, args.destination)
    if args.command == "reconcile":
        return app.reconcile(load(args.input), args.before, args.after)
    if args.command == "ingest-csv":
        return app.ingest_csv(args.id, args.source, {args.kind: args.input})
    if args.command == "export-csv":
        return app.export_csv(args.directory, seq=args.seq)
    if args.command == "decision-preview":
        return app.preview_decision(load(args.input))
    if args.command == "decision-confirm":
        return app.confirm_decision(load(args.input), [item.strip() for item in args.select.split(",") if item.strip()])
    if args.command == "decision-cancel":
        return app.cancel_reservation(args.decision,
                                      [item.strip() for item in args.actions.split(",") if item.strip()], args.at)
    if args.command == "decision-activate":
        return app.activate_reservation(args.decision,
                                        [item.strip() for item in args.actions.split(",") if item.strip()], args.at)
    if args.command == "decision-adjust":
        return app.adjust_decision(args.decision, load(args.input),
                                   [item.strip() for item in args.select.split(",") if item.strip()], args.at)
    if args.command == "decision-show":
        return app.show_decision(args.decision)
    if args.command == "decision-list":
        return app.list_decisions()
    if args.command == "decision-verify":
        return app.verify_decision(args.decision, minimum_coverage=args.minimum_coverage)
    raise DomainError("command", "操作不支持")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "verify":
            result = Publication.verify_export(args.input)
        else:
            with Application(args.database) as app:
                result = dispatch(app, args)
        print(canonical(result))
        return 0
    except DomainError as exc:
        print(canonical(exc.as_dict()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
