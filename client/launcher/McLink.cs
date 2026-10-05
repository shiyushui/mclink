// ============================================================
//  McLink 启动器 (McLink.exe)
// ============================================================
//  这是一个极小的 Windows 可执行文件，作用只有一个：
//  找到 Python，然后以「无控制台窗口」的方式拉起 mclink_gui.py。
//
//  为什么要它：直接双击 .py 会弹黑框、.bat 会闪一下、.vbs 又容易被
//  杀软误报。编译成 winexe 才是真正干净的桌面程序体验。
//
//  编译（见同目录 build_launcher.ps1）：
//      csc /target:winexe /win32icon:..\assets\mclink.ico /codepage:65001 McLink.cs
//
//  这个文件是纯 .NET Framework 2.0 语法，任何 Windows 7 以上都能跑。
// ============================================================

using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;
using Microsoft.Win32;

static class McLinkLauncher
{
    [STAThread]
    static void Main(string[] args)
    {
        string dir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string script = Path.Combine(dir, "mclink_gui.py");

        if (!File.Exists(script))
        {
            MessageBox.Show(
                "找不到 mclink_gui.py。\n\n请确保 McLink.exe 和它放在同一个文件夹里。\n\n" +
                "当前目录：" + dir,
                "McLink 启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        string python = FindPythonW();
        if (python == null)
        {
            MessageBox.Show(
                "没有找到 Python。\n\n" +
                "请先安装 Python 3.8 或更高版本：\n" +
                "    https://www.python.org/downloads/\n\n" +
                "安装时务必勾选 “Add python.exe to PATH”，\n" +
                "装完后重新双击 McLink.exe。",
                "McLink 启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        // 把参数原样转给脚本（--hidden 用于开机自启时直接进托盘）
        string argline = "\"" + script + "\"";
        foreach (string a in args)
        {
            argline += " \"" + a.Replace("\"", "\\\"") + "\"";
        }

        try
        {
            ProcessStartInfo psi = new ProcessStartInfo(python, argline);
            psi.WorkingDirectory = dir;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            Process.Start(psi);
        }
        catch (Exception ex)
        {
            MessageBox.Show("启动 McLink 失败：\n\n" + ex.Message,
                "McLink", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    /// <summary>按可靠性从高到低找一个 pythonw.exe（无控制台版本）。</summary>
    static string FindPythonW()
    {
        // 1) 环境变量 PATH
        string path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (string raw in path.Split(';'))
        {
            string d = raw.Trim();
            if (d.Length == 0) continue;
            try
            {
                string exe = Path.Combine(d, "pythonw.exe");
                if (File.Exists(exe)) return exe;
            }
            catch { }
        }

        // 2) 常见安装目录（用户级 / 全局），优先取版本号最大的
        string[] roots = new string[]
        {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                         @"Programs\Python"),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
        };
        string best = null;
        foreach (string root in roots)
        {
            if (string.IsNullOrEmpty(root) || !Directory.Exists(root)) continue;
            try
            {
                string[] dirs = Directory.GetDirectories(root, "Python3*");
                Array.Sort(dirs);
                Array.Reverse(dirs);                  // 版本号大的排前面
                foreach (string d in dirs)
                {
                    string exe = Path.Combine(d, "pythonw.exe");
                    if (File.Exists(exe) && best == null) best = exe;
                }
            }
            catch { }
        }
        if (best != null) return best;

        // 3) 注册表（官方安装包会写这里）
        try
        {
            using (RegistryKey k = Registry.CurrentUser.OpenSubKey(@"SOFTWARE\Python\PythonCore"))
            {
                if (k != null)
                {
                    string[] subs = k.GetSubKeyNames();
                    Array.Sort(subs);
                    Array.Reverse(subs);
                    foreach (string s in subs)
                    {
                        using (RegistryKey ip = k.OpenSubKey(s + @"\InstallPath"))
                        {
                            if (ip == null) continue;
                            object v = ip.GetValue("ExecutablePath");
                            if (v != null)
                            {
                                string exe = Path.Combine(
                                    Path.GetDirectoryName(v.ToString()), "pythonw.exe");
                                if (File.Exists(exe)) return exe;
                            }
                        }
                    }
                }
            }
        }
        catch { }

        // 4) 最后退回 Windows 的 py 启动器（pyw.exe 是无控制台版本）
        string windir = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
        string pyw = Path.Combine(windir, "pyw.exe");
        if (File.Exists(pyw)) return pyw;
        string py = Path.Combine(windir, "py.exe");
        if (File.Exists(py)) return py;
        return null;
    }
}
