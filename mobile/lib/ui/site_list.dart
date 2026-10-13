/// 站点列表（W00c4）：存着的站点一行一个，点了登录、进站点；右上换语言，右下添加。
/// 登录之后进的是带底部导航的外壳（`nw/nw_shell.dart`）。
library;

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/background_watch.dart';
import '../net/push.dart';
import '../net/site_client.dart';
import '../store/site_store.dart';
import 'nw/nw_shell.dart';
import 'site_add.dart';

/// 造一个站点客户端。测试换成假的。
typedef SiteApiFactory = SiteApi Function(SiteEntry site);

SiteApi defaultSiteApi(SiteEntry s) => SiteClient(s.url, s.fingerprint);

class SiteListPage extends StatefulWidget {
  final SiteStore store;
  final SiteApiFactory apiFactory;
  /// 后台值守（W17）：测试换成假的。
  final BackgroundWatch watch;
  /// P1 推到手机（商业化 A6）：测试换成假的；缺省按平台（手机上是极光，桌面版不推）。
  final PushRegistrar? push;
  /// 语言存哪儿（App V2）；不给就只在这次运行里生效。
  final LangStore? langStore;
  /// 扫配对码（App V2）：测试换成假的；缺省手机上开相机，桌面版没有扫码按钮。
  final CodeScanner? scanner;
  const SiteListPage(
      {super.key,
      required this.store,
      this.apiFactory = defaultSiteApi,
      this.watch = const ChannelBackgroundWatch(),
      this.push,
      this.langStore,
      this.scanner});

  @override
  State<SiteListPage> createState() => _SiteListPageState();
}

class _SiteListPageState extends State<SiteListPage> {
  List<SiteEntry> _sites = <SiteEntry>[];

  /// 站点文件读不出来（坏了）。这时**不许**添加或删除：一保存就把坏文件里的其余站点覆盖掉了。
  String? _loadError;

  @override
  void initState() {
    super.initState();
    widget.store.load().then((s) {
      if (mounted) setState(() => _sites = s);
    }, onError: (Object e) {
      if (mounted) setState(() => _loadError = '$e');
    });
  }

  Future<void> _setLang(AppLang lang) async {
    if (lang == appLang.value) return;
    appLang.value = lang;
    try {
      await widget.langStore?.save(lang);
    } catch (_) {
      // 存不上只是下次打开还是原来的语言，不挡这一次。
    }
  }

  Future<void> _remove(SiteEntry s) async {
    final next = [..._sites]..remove(s);
    await widget.store.save(next);
    if (mounted) setState(() => _sites = next);
  }

  /// 添加站点（App V2）：扫码、粘贴配对码或手填，都在 `SiteAddPage`；这里只管存。
  Future<void> _add() async {
    final entry = await Navigator.push<SiteEntry>(context,
        MaterialPageRoute<SiteEntry>(builder: (_) => SiteAddPage(scanner: widget.scanner)));
    if (entry == null || !mounted) return;
    final next = [..._sites, entry];
    await widget.store.save(next);
    if (mounted) setState(() => _sites = next);
  }

  Future<void> _open(SiteEntry s) async {
    final pw = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        scrollable: true, // 横屏弹键盘
        title: Text(tr('Sign in to ${s.name}', '登录 ${s.name}')),
        content: TextField(
            key: const Key('site-password'),
            controller: pw,
            obscureText: true,
            autofocus: true, // 桌面版（W15）：弹出来就能打字，回车就登录
            onSubmitted: (_) => Navigator.pop(c, true),
            decoration: InputDecoration(
                labelText: tr('Password for ${s.username}', '${s.username} 的口令'))),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '取消'))),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: Text(tr('Sign in', '登录'))),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    final messenger = ScaffoldMessenger.of(context);
    final nav = Navigator.of(context);
    SiteApi? api;
    try {
      api = widget.apiFactory(s);
      await api.login(s.username, pw.text);
    } catch (e) {
      api?.close();
      messenger.showSnackBar(SnackBar(content: Text(tr("Can't sign in: $e", '登录不了：$e'))));
      return;
    }
    final a = api;
    // 商业化 A6：进了站点就收这个站点的 P1 推送。登记不上不挡进站点（后台值守照旧），退避一直重试，
    // 页面上看得见没登记上（A6 外审 F2）。
    final push = PushSession(widget.push ?? defaultPush(),
        also: widget.push == null ? defaultAlsoPush() : const <PushRegistrar>[],
        register: (id, platform, provider) => a.pushRegister(id, platform, provider: provider),
        unregister: a.pushUnregister)
      ..start();
    final why = await nav.push<String>(MaterialPageRoute<String>(
        builder: (_) => NwShell(
            api: a, title: s.name, entry: s, watch: widget.watch, push: push)));
    // 离开就注销：不然令牌在站点上还能用半小时。推送先停、注销推送号（在路上的登记落定了再注销，
    // A6 外审 F3），再注销会话（站点那头推送号跟这次登录绑着，退出了也不再推）。
    await push.stop();
    try {
      if (a.session != null) await a.logout();
    } catch (_) {}
    a.close();
    if (why != null) messenger.showSnackBar(SnackBar(content: Text(why)));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(tr('Sites', '站点')), actions: [
        // 语言（App V2）：英文为主，中文可选。只在这一页能换 —— 换了整个 app 重建。
        PopupMenuButton<AppLang>(
            key: const Key('lang-switch'),
            tooltip: tr('Language', '语言'),
            icon: const Icon(Icons.language),
            initialValue: appLang.value,
            onSelected: _setLang,
            itemBuilder: (_) => const [
                  PopupMenuItem(value: AppLang.en, child: Text('English')),
                  PopupMenuItem(value: AppLang.zh, child: Text('中文')),
                ]),
      ]),
      floatingActionButton: _loadError != null
          ? null
          : FloatingActionButton(
              key: const Key('site-add'), onPressed: _add, child: const Icon(Icons.add)),
      body: _loadError != null
          ? Center(
              child: Text(tr("Can't read the site list: $_loadError", '站点列表读不出来：$_loadError'),
                  key: const Key('site-load-error')))
          : _sites.isEmpty
              ? Center(
                  child: Text(tr('No sites yet. Add one with the button at the bottom right.',
                      '还没有站点。右下角添加。')))
              : ListView(children: [
                  for (final s in _sites)
                    ListTile(
                        title: Text(s.name),
                        subtitle: Text('${s.url} · ${s.username}'),
                        trailing: IconButton(
                            key: Key('site-remove-${s.name}'),
                            tooltip: tr('Remove (after the certificate is reissued, remove and add again)',
                                '删掉（证书重签后删了重加）'),
                            icon: const Icon(Icons.delete_outline),
                            onPressed: () => _remove(s)),
                        onTap: () => _open(s)),
                ]),
    );
  }
}
