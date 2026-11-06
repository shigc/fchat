; Fchat 安装包脚本（Inno Setup 6）
; 用法：iscc /DAPP_VERSION=1.0.0 fchat.iss
; 不设 [InstallDelete]：同 AppId 升级时 Inno 先跑旧版卸载清走旧文件，
; 再全清 {app} 的话，一旦复制阶段失败就留下"已卸载、新文件没进来"的
; 半卸载状态（1.0.2 真机踩坑）。ignoreversion 足以覆盖旧 .pyd。

#define MyAppName "Fchat"
#define MyAppExeName "fchat.exe"

#ifndef APP_VERSION
#define APP_VERSION "1.0.0"
#endif

[Setup]
AppId={{7C1E9F30-4A82-4B0F-9C4E-3E5D6A7F8B21}}
AppName={#MyAppName}
AppVersion={#APP_VERSION}
AppPublisher={#MyAppName}
DefaultDirName={autopf}\Fchat
DefaultGroupName={#MyAppName}
OutputDir=..\build\output
OutputBaseFilename=Fchat-{#APP_VERSION}-setup
SetupIconFile=..\resources\fchat.ico
; GPL-2.0 要求二进制分发时随附许可文本：向导首屏展示，并把两个文本装进 {app}
LicenseFile=..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; 更新流程依赖静默安装；装完不自动拉起（应用未注册 Restart Manager）
CloseApplications=yes
RestartApplications=no
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "chs"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; \
    GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "..\build\dist\fchat\*"; DestDir: "{app}"; \
    Flags: recursesubdirs createallsubdirs ignoreversion
; 许可文本随二进制一起交付（GPLv2 第 1 条）+ 第三方组件清单
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_LICENSES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "运行 {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

[Code]
const
  UPTMP_EXT = '.uptmp';

// 改名需要目标的 DELETE 权限：任何未开 FILE_SHARE_DELETE 的句柄都会让
// 改名失败——与复制阶段 DeleteFile 的冲突完全同源，所以逐文件改名能精确
// 预演复制是否会因占用而失败。不能改成整目录改名：cmd / Explorer 的工作
// 目录句柄只锁目录本身、不影响文件复制（1.0.5 真机踩坑——旧客户端的
// cmd CWD 在 {app} 上导致整目录改名 8 次重试全败、升级被误杀）。
function ProbeFile(const F: String; var FailedFile: String): Boolean;
var
  Tmp: String;
begin
  Tmp := F + UPTMP_EXT;
  if FileExists(Tmp) then
    DeleteFile(Tmp);
  if not RenameFile(F, Tmp) then
  begin
    FailedFile := F;
    Result := False;
    Exit;
  end;
  // 改回去失败理论上不可能（刚改名成功、期间无人持有新名句柄），一旦发生
  // 只能如实报告
  if not RenameFile(Tmp, F) then
  begin
    FailedFile := Tmp;
    Result := False;
    Exit;
  end;
  Result := True;
end;

function ProbeTree(const Dir: String; var FailedFile: String): Boolean;
var
  FR: TFindRec;
  F: String;
begin
  Result := True;
  if FindFirst(Dir + '\*', FR) then
  begin
    try
      repeat
        if (FR.Name <> '.') and (FR.Name <> '..') then
        begin
          F := Dir + '\' + FR.Name;
          if (FR.Attributes and $10) <> 0 then   // $10 = FILE_ATTRIBUTE_DIRECTORY
          begin
            if not ProbeTree(F, FailedFile) then
            begin
              Result := False;
              Exit;
            end;
          end
          else if not ProbeFile(F, FailedFile) then
          begin
            Result := False;
            Exit;
          end;
        end;
      until not FindNext(FR);
    finally
      FindClose(FR);
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  AppDir, FailedFile: String;
  I: Integer;
begin
  Result := '';
  AppDir := ExpandConstant('{app}');
  // 运行中的 fchat.exe 会锁住 _internal 的 dll/pyd，杀软等外部进程也可能
  // 短暂占住数据文件（cacert.pem 真机踩坑）；而 Inno 在复制阶段才失败的话
  // 旧版卸载已经跑完，回滚即半卸载、不可恢复。所以必须在卸载旧版之前把
  // 占用问题全部暴露掉：先强杀 fchat.exe，再对 {app} 逐文件改名探测。
  // 探测不过就返回错误串，Setup 会在触碰旧版之前干净退出
  // （最坏情况=升级失败但旧版完好）。
  for I := 1 to 3 do
  begin
    Exec(ExpandConstant('{sys}\taskkill.exe'),
         '/F /IM fchat.exe /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(500);
  end;
  if not DirExists(AppDir) then
    Exit;
  FailedFile := '';
  for I := 1 to 8 do
  begin
    if ProbeTree(AppDir, FailedFile) then
      Exit;
    Sleep(1500);
  end;
  Result := 'Fchat 的安装文件正被其他程序占用，无法安全升级。' #13#10
            '旧版本未受影响，请关闭相关程序后重新运行升级。' #13#10
            '（占用文件：' + FailedFile + '）';
end;
