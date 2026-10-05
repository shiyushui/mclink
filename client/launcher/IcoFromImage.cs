// ============================================================
//  IcoFromImage —— 把一张图片转成 Windows 多尺寸 ICO
// ============================================================
//  为什么要用它：Python 标准库解不了 JPEG，又不想为了一个图标
//  引入 Pillow。用系统自带的 .NET 图形库 + csc 编译，零依赖。
//
//  功能：
//    - 按 16/20/24/32/40/48/64/128/256 逐级缩放
//    - 自动把白色背景抠成透明（像素风图标的圆角就出来了）
//    - 输出**经典 BMP 格式**的多尺寸 ICO
//      （注意：csc /win32icon 不认 Vista 之后流行的 PNG-in-ICO）
//
//  编译运行见 build_icon.ps1
// ============================================================

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;

static class IcoFromImage
{
    static readonly int[] Sizes = { 16, 20, 24, 32, 40, 48, 64, 128, 256 };

    [STAThread]
    static int Main(string[] args)
    {
        if (args.Length < 2)
        {
            Console.WriteLine("用法: IcoFromImage <输入图> <输出.ico> [预览.png] [白底阈值=238]");
            return 2;
        }
        string src = args[0], dst = args[1];
        string preview = args.Length > 2 && args[2] != "-" ? args[2] : null;
        int thresh = args.Length > 3 ? int.Parse(args[3]) : 238;

        using (Bitmap origin = new Bitmap(src))
        {
            Console.WriteLine("源图: " + origin.Width + "x" + origin.Height + " " + origin.PixelFormat);

            var dims = new List<int>();
            var blobs = new List<byte[]>();
            string outDir = Path.GetDirectoryName(Path.GetFullPath(dst));

            foreach (int s in Sizes)
            {
                using (Bitmap scaled = Resize(origin, s))
                {
                    KeyOutWhite(scaled, thresh);
                    blobs.Add(ToDib(scaled));
                    dims.Add(s);
                    // 顺便导出几档 PNG：tkinter 只认 PNG，读不了 ico
                    if (s == 32 || s == 64 || s == 256)
                        scaled.Save(Path.Combine(outDir, "mclink-" + s + ".png"),
                                    ImageFormat.Png);
                    if (preview != null && s == 256) scaled.Save(preview, ImageFormat.Png);
                }
            }
            WriteIco(dst, dims, blobs);
            Console.WriteLine("生成 " + dims.Count + " 个尺寸 -> " + dst);
        }
        return 0;
    }

    /// <summary>缩放到正方形；先铺白底再画，避免边缘出现黑边（之后再抠成透明）。</summary>
    static Bitmap Resize(Bitmap origin, int size)
    {
        var bmp = new Bitmap(size, size, PixelFormat.Format32bppArgb);
        using (var g = Graphics.FromImage(bmp))
        {
            g.Clear(Color.White);
            g.InterpolationMode = InterpolationMode.HighQualityBicubic;
            g.PixelOffsetMode = PixelOffsetMode.HighQuality;
            g.SmoothingMode = SmoothingMode.HighQuality;
            g.CompositingQuality = CompositingQuality.HighQuality;
            g.DrawImage(origin, new Rectangle(0, 0, size, size));
        }
        return bmp;
    }

    /// <summary>把接近纯白、且没什么饱和度的像素变透明，并在过渡带做一点羽化。</summary>
    static void KeyOutWhite(Bitmap bmp, int thresh)
    {
        Rectangle rect = new Rectangle(0, 0, bmp.Width, bmp.Height);
        BitmapData data = bmp.LockBits(rect, ImageLockMode.ReadWrite, PixelFormat.Format32bppArgb);
        int total = data.Stride * bmp.Height;
        byte[] buf = new byte[total];
        Marshal.Copy(data.Scan0, buf, 0, total);

        int band = 36;                                  // 过渡带宽度
        for (int y = 0; y < bmp.Height; y++)
        {
            int row = y * data.Stride;
            for (int x = 0; x < bmp.Width; x++)
            {
                int i = row + x * 4;
                int b = buf[i], g = buf[i + 1], r = buf[i + 2];
                int mn = Math.Min(r, Math.Min(g, b));
                int mx = Math.Max(r, Math.Max(g, b));
                int lum = (r + g + b) / 3;
                int sat = mx - mn;

                int a;
                if (lum >= thresh && sat <= 14) a = 0;
                else if (lum >= thresh - band && sat <= 40)
                    a = (int)(255.0 * (thresh - lum) / band);
                else a = 255;

                if (a < 0) a = 0;
                if (a > 255) a = 255;
                buf[i + 3] = (byte)a;
            }
        }
        Marshal.Copy(buf, 0, data.Scan0, total);
        bmp.UnlockBits(data);
    }

    /// <summary>转成 ICO 内部的 32 位 DIB（BITMAPINFOHEADER + 自下而上的 BGRA + AND 掩码）。</summary>
    static byte[] ToDib(Bitmap bmp)
    {
        int w = bmp.Width, h = bmp.Height;
        BitmapData data = bmp.LockBits(new Rectangle(0, 0, w, h),
                                       ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
        byte[] px = new byte[data.Stride * h];
        Marshal.Copy(data.Scan0, px, 0, px.Length);
        bmp.UnlockBits(data);

        int xorSize = w * h * 4;
        int maskStride = ((w + 31) / 32) * 4;

        MemoryStream ms = new MemoryStream();
        BinaryWriter bw = new BinaryWriter(ms);
        bw.Write(40);            // biSize
        bw.Write(w);             // biWidth
        bw.Write(h * 2);         // biHeight（XOR + AND 所以翻倍）
        bw.Write((short)1);      // biPlanes
        bw.Write((short)32);     // biBitCount
        bw.Write(0);             // biCompression = BI_RGB
        bw.Write(xorSize);       // biSizeImage
        bw.Write(0); bw.Write(0); bw.Write(0); bw.Write(0);

        for (int y = h - 1; y >= 0; y--)                // DIB 自下而上
        {
            int row = y * data.Stride;
            for (int x = 0; x < w; x++)
            {
                int i = row + x * 4;
                bw.Write(px[i + 0]);                    // B
                bw.Write(px[i + 1]);                    // G
                bw.Write(px[i + 2]);                    // R
                bw.Write(px[i + 3]);                    // A
            }
        }
        bw.Write(new byte[maskStride * h]);             // AND 掩码全 0，透明交给 alpha
        bw.Flush();
        return ms.ToArray();
    }

    static void WriteIco(string path, List<int> dims, List<byte[]> blobs)
    {
        using (FileStream fs = File.Create(path))
        using (BinaryWriter bw = new BinaryWriter(fs))
        {
            bw.Write((short)0);                         // reserved
            bw.Write((short)1);                         // type = icon
            bw.Write((short)dims.Count);

            int offset = 6 + 16 * dims.Count;
            for (int i = 0; i < dims.Count; i++)
            {
                int d = dims[i] >= 256 ? 0 : dims[i];   // 256 记作 0
                bw.Write((byte)d);                      // bWidth
                bw.Write((byte)d);                      // bHeight
                bw.Write((byte)0);                      // bColorCount
                bw.Write((byte)0);                      // bReserved
                bw.Write((short)1);                     // wPlanes
                bw.Write((short)32);                    // wBitCount
                bw.Write(blobs[i].Length);              // dwBytesInRes
                bw.Write(offset);                       // dwImageOffset
                offset += blobs[i].Length;
            }
            foreach (byte[] b in blobs) bw.Write(b);
        }
    }
}
