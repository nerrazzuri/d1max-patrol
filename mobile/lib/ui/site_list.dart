/// 站点列表（W00c4）：存着的站点一个一张卡，点了去登录页、登上了进站点；右上换语言、添加，底下也能添加。
/// 登录之后进的是带底部导航的外壳（`nw/nw_shell.dart`）。
/// 2026-10-14 按设计稿重做（P1）：选站点是设计稿的 Site Selection，登录从弹窗改成一页（[SiteLoginPage]）。
library;

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/background_watch.dart';
import '../net/push.dart';
import '../net/site_client.dart';
import '../store/site_store.dart';
import 'nw/nw_button.dart';
import 'nw/nw_cards.dart';
import 'nw/nw_p1_entry.dart';
import 'nw/nw_shell.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'robot_bind_page.dart';
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

  /// 卡片最宽这么多（桌面版的大窗口上不拉满）。
  static const double _maxWidth = 720;

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
    String? account0;
    final messenger = ScaffoldMessenger.of(context);
    final nav = Navigator.of(context);
    // 口令在登录页上收（2026-10-14 重做：原来是弹窗）。登录不了人留在那一页、原因摆在那一页上，
    // 改了能再试；登上了那一页带着连好的客户端退回来，下面的顺序跟原来一样。
    final api = await nav.push<SiteApi>(MaterialPageRoute<SiteApi>(
        builder: (_) => SiteLoginPage(
            site: s,
            signIn: (account, password) async {
              SiteApi? api;
              try {
                api = widget.apiFactory(s);
                await api.login(account, password);
              } catch (_) {
                api?.close();
                rethrow;
              }
              account0 = account;
              return api;
            })));
    if (api == null) return;
    final a = api;
    // 记下这次登录用的账号（下次登录页自动填好）
    if (account0 != null && account0 != s.username) {
      final upd = SiteEntry(name: s.name, url: s.url, fingerprint: s.fingerprint, username: account0!);
      final next = [for (final x in _sites) identical(x, s) ? upd : x];
      await widget.store.save(next);
      if (mounted) setState(() => _sites = next);
      s = upd;
    }
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
    final c = Nw.of(context);
    final broken = _loadError != null;
    return Scaffold(
      backgroundColor: c.bg,
      // 页头、底栏各占 Scaffold 的一格（不是 body 里的 Column）：登录页键盘还弹着就退回来的那一下，
      // body 可以只剩零高，这样摆不溢出。
      appBar: nwHeaderBar(
          context,
          NwHeader(overline: 'Nexus Warden', title: tr('Site Selection', '选择站点'), trailing: [
            // 语言（App V2）：英文为主，中文可选。只在这一页能换 —— 换了整个 app 重建。
            PopupMenuButton<AppLang>(
                key: const Key('lang-switch'),
                tooltip: tr('Language', '语言'),
                icon: Icon(Icons.language_rounded, color: c.text),
                initialValue: appLang.value,
                onSelected: _setLang,
                itemBuilder: (_) => const [
                      PopupMenuItem(value: AppLang.en, child: Text('English')),
                      PopupMenuItem(value: AppLang.zh, child: Text('中文')),
                    ]),
            // W42：工程师工具 —— 站点坏了没备份，拿公司签的解绑令解绑机器人（不用登录站点）
            IconButton(
                key: const Key('engineer-unbind'),
                tooltip: tr('Engineer: unbind a robot with a token', '工程师：用解绑令解绑机器人'),
                icon: Icon(Icons.build_circle_outlined, color: c.text),
                onPressed: () => Navigator.push(
                    context, MaterialPageRoute<void>(builder: (_) => const EngineerUnbindPage()))),
            if (!broken)
              IconButton(
                  key: const Key('site-add-top'),
                  tooltip: tr('Add site', '添加站点'),
                  icon: Icon(Icons.add_circle_outline_rounded, size: 28, color: c.text),
                  onPressed: _add),
          ])),
      // 站点文件坏了不给添加（见 _loadError）
      bottomNavigationBar: broken
          ? null
          : Container(
              decoration:
                  BoxDecoration(color: c.surface, border: Border(top: BorderSide(color: c.outline))),
              child: SafeArea(
                  top: false,
                  child: Padding(
                      padding: const EdgeInsets.symmetric(horizontal: Nw.s24, vertical: Nw.s16),
                      child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                        Flexible(
                            child: ConstrainedBox(
                                constraints: const BoxConstraints(maxWidth: _maxWidth - 2 * Nw.s24),
                                child: NwButton(
                                    buttonKey: const Key('site-add'),
                                    label: tr('Add New Station', '添加新站点'),
                                    icon: Icons.qr_code_scanner_rounded,
                                    variant: NwButtonVariant.outline,
                                    fullWidth: true,
                                    onPressed: _add))),
                      ])))),
      body: SafeArea(
          top: false,
          bottom: false,
          child: broken
              ? Center(
                  child: SingleChildScrollView(
                      padding: const EdgeInsets.all(Nw.s24),
                      child: Column(mainAxisSize: MainAxisSize.min, children: [
                        Icon(Icons.error_outline_rounded, size: 48, color: c.error),
                        const SizedBox(height: Nw.s16),
                        Text(tr("Can't read the site list: $_loadError", '站点列表读不出来：$_loadError'),
                            key: const Key('site-load-error'),
                            textAlign: TextAlign.center,
                            style: NwText.bodyMedium.copyWith(color: c.text)),
                      ])))
              : _sites.isEmpty
                  ? NwEmpty(
                      icon: Icons.domain_add_rounded,
                      text: tr('No sites yet. Add one with the button at the bottom right.',
                          '还没有站点。右下角添加。'),
                      hint: tr('Scan the site code shown on the duty console, or enter the address by hand.',
                          '扫值班台上的配对码，或者手填站点地址。'))
                  // 宽屏（桌面版）上卡片不拉满一整行
                  : Align(
                      alignment: Alignment.topCenter,
                      child: ConstrainedBox(
                          constraints: const BoxConstraints(maxWidth: _maxWidth),
                          child: ListView.separated(
                              padding: const EdgeInsets.all(Nw.s24),
                              itemCount: _sites.length,
                              separatorBuilder: (_, _) => const SizedBox(height: Nw.s16),
                              itemBuilder: (_, i) => _card(c, _sites[i]))))),
    );
  }

  /// 一个站点一张卡。登录之前不知道连没连上、有没有 P1：卡上不显示这两样（不编）。
  Widget _card(NwColors c, SiteEntry s) => NwSiteCard(
      name: s.name,
      address: s.username.isEmpty ? s.url : '${s.url} · ${s.username}',
      enterKey: Key('site-enter-${s.name}'),
      onEnter: () => _open(s),
      menu: PopupMenuButton<bool>(
          key: Key('site-menu-${s.name}'),
          tooltip: tr('More', '更多'),
          icon: Icon(Icons.more_vert_rounded, color: c.textSecondary),
          onSelected: (_) => _remove(s),
          itemBuilder: (_) => [
                PopupMenuItem<bool>(
                    key: Key('site-remove-${s.name}'),
                    value: true,
                    child: Text(
                        tr('Remove (after the certificate is reissued, remove and add again)',
                            '删掉（证书重签后删了重加）'),
                        style: NwText.bodyMedium.copyWith(color: c.error))),
              ]));
}

/// 登录这个站点：拿口令去登，登上了回连好的客户端，登不上抛（原因给人看）。
/// 用账号、口令登录（账号添加站点时不填，第一次登录时输，之后记着；用户 10-30）。
typedef SiteSignIn = Future<SiteApi> Function(String account, String password);

/// 登录页（2026-10-14 重做，原来是弹窗）：品牌、要登的是哪个站点、口令。账号是加站点时存下的，这里
/// 只填口令。登上了带着客户端退回去（`Navigator.pop(context, api)`）；登不上留在这一页说原因。
/// 设计稿上的数字键盘、指纹、「Emergency Station Reset」没有做（用户 2026-10-14 定的）。
class SiteLoginPage extends StatefulWidget {
  final SiteEntry site;
  final SiteSignIn signIn;
  const SiteLoginPage({super.key, required this.site, required this.signIn});

  @override
  State<SiteLoginPage> createState() => _SiteLoginPageState();
}

class _SiteLoginPageState extends State<SiteLoginPage> {
  late final _account = TextEditingController(text: widget.site.username);
  final _pw = TextEditingController();

  /// 正在登：这时候不收第二次提交，也不许退出去（退了登上的那个会话就没人管了）。
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _account.dispose();
    _pw.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (_busy) return;
    if (_account.text.trim().isEmpty) {
      setState(() => _error = tr('Enter your account', '填你的账号'));
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    final SiteApi api;
    try {
      api = await widget.signIn(_account.text.trim(), _pw.text);
    } catch (e) {
      if (mounted) {
        setState(() {
          _busy = false;
          _error = tr("Can't sign in: $e", '登录不了：$e');
        });
      }
      return;
    }
    if (!mounted) {
      // 页面已经没了（不该发生：正在登的时候退不出去）。登上的会话不留着：注销、关掉。
      try {
        await api.logout();
      } catch (_) {}
      api.close();
      return;
    }
    Navigator.pop(context, api);
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final s = widget.site;
    final error = _error;
    return PopScope(
      canPop: !_busy,
      child: Scaffold(
        backgroundColor: c.bg,
        // 横屏弹键盘剩不了多高：整页能滚
        body: SafeArea(
            child: Center(
                child: SingleChildScrollView(
                    padding: const EdgeInsets.all(Nw.s24),
                    child: ConstrainedBox(
                        constraints: const BoxConstraints(maxWidth: 420),
                        child: Column(
                            mainAxisSize: MainAxisSize.min,
                            crossAxisAlignment: CrossAxisAlignment.stretch,
                            children: [
                              // 品牌
                              Center(
                                  child: Container(
                                      width: 64,
                                      height: 64,
                                      alignment: Alignment.center,
                                      decoration: BoxDecoration(
                                          color: c.surface,
                                          borderRadius: BorderRadius.circular(Nw.r12),
                                          border: Border.all(color: c.outline)),
                                      child: Icon(Icons.security_rounded, size: 32, color: c.primary))),
                              const SizedBox(height: Nw.s16),
                              Text('Nexus Warden',
                                  textAlign: TextAlign.center,
                                  style: NwText.titleLarge.copyWith(color: c.text, fontWeight: FontWeight.w700)),
                              const SizedBox(height: Nw.s4),
                              Text(tr('Security Command', '安防指挥'),
                                  textAlign: TextAlign.center,
                                  style: NwText.bodySmall.copyWith(color: c.textSecondary)),
                              const SizedBox(height: Nw.s32),
                              // 要登的是哪个站点；「换站点」回列表
                              Container(
                                  padding: const EdgeInsets.fromLTRB(Nw.s16, Nw.s8, Nw.s8, Nw.s8),
                                  decoration: BoxDecoration(
                                      color: c.surface,
                                      borderRadius: BorderRadius.circular(Nw.r8),
                                      border: Border.all(color: c.outline)),
                                  child: Row(children: [
                                    Icon(Icons.location_on_rounded, size: 20, color: c.textSecondary),
                                    const SizedBox(width: Nw.s16),
                                    Expanded(
                                        child: Column(
                                            mainAxisSize: MainAxisSize.min,
                                            crossAxisAlignment: CrossAxisAlignment.start,
                                            children: [
                                          Text(s.name,
                                              maxLines: 1,
                                              overflow: TextOverflow.ellipsis,
                                              style: NwText.labelLarge.copyWith(color: c.text)),
                                          const SizedBox(height: Nw.s4),
                                          // 站点地址长了折一行（两行为止）
                                          Text(s.url,
                                              maxLines: 2,
                                              overflow: TextOverflow.ellipsis,
                                              style: NwText.labelSmall.copyWith(color: c.textSecondary)),
                                        ])),
                                    const SizedBox(width: Nw.s8),
                                    NwButton(
                                        buttonKey: const Key('login-switch'),
                                        label: tr('Switch', '换站点'),
                                        variant: NwButtonVariant.ghost,
                                        size: NwButtonSize.small,
                                        onPressed: _busy ? null : () => Navigator.pop(context)),
                                  ])),
                              const SizedBox(height: Nw.s32),
                              NwField(
                                  fieldKey: const Key('site-account'),
                                  controller: _account,
                                  label: tr('Account', '账号'),
                                  icon: Icons.person_outline_rounded,
                                  autocorrect: false,
                                  enableSuggestions: false,
                                  autofocus: s.username.isEmpty),
                              const SizedBox(height: Nw.s16),
                              NwField(
                                  fieldKey: const Key('site-password'),
                                  controller: _pw,
                                  label: tr('Password', '口令'),
                                  icon: Icons.lock_outline_rounded,
                                  obscureText: true,
                                  autofocus: s.username.isNotEmpty, // 桌面版（W15）：一进来就能打字，回车就登录
                                  autocorrect: false,
                                  enableSuggestions: false,
                                  error: error != null,
                                  onSubmitted: (_) => _submit()),
                              if (error != null) ...[
                                const SizedBox(height: Nw.s16),
                                NwNotice(key: const Key('site-login-error'), text: error, tone: NwTone.error),
                              ],
                              const SizedBox(height: Nw.s24),
                              NwButton(
                                  buttonKey: const Key('site-login'),
                                  label: _busy ? tr('Signing in…', '正在登录…') : tr('Sign in', '登录'),
                                  size: NwButtonSize.large,
                                  fullWidth: true,
                                  onPressed: _busy ? null : _submit),
                            ]))))),
      ),
    );
  }
}
