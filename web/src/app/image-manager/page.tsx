"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CalendarDays, ChevronLeft, ChevronRight, Copy, ImageIcon, LoaderCircle, Maximize2, RefreshCw, Search, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { DateRangeFilter } from "@/components/date-range-filter";
import { ImageLightbox } from "@/components/image-lightbox";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  cleanupStoredImages,
  deleteStoredImages,
  fetchManagedImages,
  fetchStorageUsage,
  type ManagedImage,
  type StorageUsage,
} from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";

function formatSize(size: number) {
  if (size >= 1024 * 1024 * 1024) {
    return `${(size / 1024 / 1024 / 1024).toFixed(2)} GB`;
  }
  return size > 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(2)} MB` : `${Math.ceil(size / 1024)} KB`;
}

function ImageManagerContent() {
  const [items, setItems] = useState<ManagedImage[]>([]);
  const [usage, setUsage] = useState<StorageUsage | null>(null);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [lightboxIndex, setLightboxIndex] = useState(0);
  const [lightboxOpen, setLightboxOpen] = useState(false);
  const [page, setPage] = useState(1);
  const [dimensions, setDimensions] = useState<Record<string, string>>({});
  const [selectedPaths, setSelectedPaths] = useState<string[]>([]);
  const [clearAllOpen, setClearAllOpen] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isCleaning, setIsCleaning] = useState(false);
  const lightboxImages = items.map((item) => ({
    id: item.name,
    src: item.url,
    sizeLabel: formatSize(item.size),
    dimensions: dimensions[item.url],
  }));
  const pageSize = 12;
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize));
  const safePage = Math.min(page, pageCount);
  const currentRows = items.slice((safePage - 1) * pageSize, safePage * pageSize);

  const selectablePaths = useMemo(
    () => items.map((item) => item.path).filter((path): path is string => Boolean(path)),
    [items],
  );

  const loadImages = useCallback(async () => {
    setIsLoading(true);
    try {
      const [data, storage] = await Promise.all([
        fetchManagedImages({ start_date: startDate, end_date: endDate }),
        fetchStorageUsage(),
      ]);
      setItems(data.items);
      setUsage(storage);
      setSelectedPaths((prev) => prev.filter((path) => data.items.some((item) => item.path === path)));
      setPage(1);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载图片失败");
    } finally {
      setIsLoading(false);
    }
  }, [endDate, startDate]);

  const handleDeletePaths = async (paths: string[]) => {
    if (paths.length === 0) {
      toast.error("请先选择要删除的图片");
      return;
    }
    setIsCleaning(true);
    try {
      const result = await deleteStoredImages(paths);
      toast.success(`已删除 ${result.removed_files} 张，释放 ${formatSize(result.freed_bytes)}`);
      setSelectedPaths([]);
      await loadImages();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除图片失败");
    } finally {
      setIsCleaning(false);
    }
  };

  const handleCleanup = async (olderThanDays: number) => {
    setIsCleaning(true);
    try {
      const result = await cleanupStoredImages({ older_than_days: olderThanDays });
      toast.success(`已清理 ${result.removed_files} 个文件，释放 ${formatSize(result.freed_bytes)}`);
      setClearAllOpen(false);
      setSelectedPaths([]);
      await loadImages();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "清理缓存失败");
    } finally {
      setIsCleaning(false);
    }
  };

  const clearFilters = () => {
    setStartDate("");
    setEndDate("");
  };

  useEffect(() => {
    void loadImages();
  }, [loadImages]);

  return (
    <section className="space-y-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="space-y-1">
          <div className="text-xs font-semibold tracking-[0.18em] text-stone-500 uppercase">Images</div>
          <h1 className="text-2xl font-semibold tracking-tight">图片管理</h1>
        </div>
        <div className="flex flex-wrap gap-2">
          <DateRangeFilter startDate={startDate} endDate={endDate} onChange={(start, end) => { setStartDate(start); setEndDate(end); }} />
          <Button variant="outline" onClick={clearFilters} className="h-10 rounded-xl border-stone-200 bg-white px-4 text-stone-700">
            清除筛选条件
          </Button>
          <Button onClick={() => void loadImages()} disabled={isLoading} className="h-10 rounded-xl bg-stone-950 px-4 text-white hover:bg-stone-800">
            {isLoading ? <LoaderCircle className="size-4 animate-spin" /> : <Search className="size-4" />}
            查询
          </Button>
          <Button
            variant="outline"
            className="h-10 rounded-xl border-stone-200 bg-white px-4 text-stone-700"
            onClick={() => void handleCleanup(usage?.retention_days || 30)}
            disabled={isLoading || isCleaning}
          >
            {isCleaning ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
            清理 {usage?.retention_days || 30} 天前
          </Button>
          <Button
            variant="outline"
            className="h-10 rounded-xl border-rose-200 bg-white px-4 text-rose-600 hover:bg-rose-50"
            onClick={() => setClearAllOpen(true)}
            disabled={isLoading || isCleaning}
          >
            <Trash2 className="size-4" />
            清空图片缓存
          </Button>
        </div>
      </div>

      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="p-0">
          <div className="flex items-center justify-between border-b border-stone-100 px-5 py-4">
            <div className="flex flex-wrap items-center gap-3 text-sm text-stone-600">
              <div className="flex items-center gap-2">
                <ImageIcon className="size-4" />
                列表 {items.length} 张
              </div>
              {usage ? (
                <span className="text-stone-500">
                  缓存 {formatSize(usage.total_bytes)} · 生成图 {formatSize(usage.images.bytes)} · 资产 {formatSize(usage.assets.bytes)}
                </span>
              ) : null}
              {selectablePaths.length > 0 ? (
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-stone-500 hover:bg-stone-100"
                  onClick={() => {
                    const pagePaths = currentRows
                      .map((item) => item.path)
                      .filter((path): path is string => Boolean(path));
                    const allSelected = pagePaths.every((path) => selectedPaths.includes(path));
                    setSelectedPaths((prev) =>
                      allSelected
                        ? prev.filter((path) => !pagePaths.includes(path))
                        : Array.from(new Set([...prev, ...pagePaths])),
                    );
                  }}
                >
                  本页全选
                </Button>
              ) : null}
              {selectedPaths.length > 0 ? (
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-rose-600 hover:bg-rose-50"
                  onClick={() => void handleDeletePaths(selectedPaths)}
                  disabled={isCleaning}
                >
                  <Trash2 className="size-4" />
                  删除所选 {selectedPaths.length}
                </Button>
              ) : null}
            </div>
            <Button variant="ghost" className="h-8 rounded-lg px-3 text-stone-500" onClick={() => void loadImages()} disabled={isLoading}>
              <RefreshCw className={`size-4 ${isLoading ? "animate-spin" : ""}`} />
              刷新
            </Button>
          </div>
          <div className="grid gap-0 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {currentRows.map((item, index) => {
              const imageIndex = items.findIndex((row) => row.url === item.url);
              return (
              <div key={item.url} className="group border-r border-b border-stone-100 p-4 transition hover:bg-stone-50">
                {item.path ? (
                  <div className="mb-2">
                    <Checkbox
                      checked={selectedPaths.includes(item.path)}
                      onCheckedChange={(checked) => {
                        setSelectedPaths((prev) =>
                          checked
                            ? Array.from(new Set([...prev, item.path as string]))
                            : prev.filter((path) => path !== item.path),
                        );
                      }}
                    />
                  </div>
                ) : null}
                <button
                  type="button"
                  className="relative block aspect-square w-full cursor-zoom-in overflow-hidden rounded-lg bg-stone-100 text-left"
                  onClick={() => {
                    setLightboxIndex(imageIndex);
                    setLightboxOpen(true);
                  }}
                >
                  <img
                    src={item.url}
                    alt={item.name}
                    className="h-full w-full object-cover transition group-hover:scale-[1.02]"
                    onLoad={(event) => {
                      const image = event.currentTarget;
                      setDimensions((current) => ({
                        ...current,
                        [item.url]: `${image.naturalWidth} x ${image.naturalHeight}`,
                      }));
                    }}
                  />
                  <span className="absolute right-2 bottom-2 rounded-full bg-black/50 p-2 text-white opacity-0 transition group-hover:opacity-100">
                    <Maximize2 className="size-4" />
                  </span>
                </button>
                <div className="mt-3 space-y-1 text-xs text-stone-500">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-1 font-medium text-stone-700">
                      <CalendarDays className="size-3.5" />
                      {item.created_at}
                    </div>
                    <div className="flex items-center gap-1">
                      {item.path ? (
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-8 rounded-lg text-stone-400 hover:bg-rose-50 hover:text-rose-500"
                          onClick={() => void handleDeletePaths([item.path as string])}
                          disabled={isCleaning}
                        >
                          <Trash2 className="size-4" />
                        </Button>
                      ) : null}
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-8 rounded-lg text-stone-400 hover:bg-stone-100 hover:text-stone-700"
                        onClick={() => {
                          void navigator.clipboard.writeText(item.url);
                          toast.success("图片地址已复制");
                        }}
                      >
                        <Copy className="size-4" />
                      </Button>
                    </div>
                  </div>
                  <div className="flex items-center justify-between gap-2">
                    <span>{formatSize(item.size)}</span>
                    <span>{dimensions[item.url] || "-"}</span>
                  </div>
                </div>
              </div>
            )})}
          </div>
          <div className="flex items-center justify-end gap-2 border-t border-stone-100 px-4 py-3 text-sm text-stone-500">
            <span>第 {safePage} / {pageCount} 页，共 {items.length} 张</span>
            <Button variant="outline" size="icon" className="size-9 rounded-lg border-stone-200 bg-white" disabled={safePage <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>
              <ChevronLeft className="size-4" />
            </Button>
            <Button variant="outline" size="icon" className="size-9 rounded-lg border-stone-200 bg-white" disabled={safePage >= pageCount} onClick={() => setPage((value) => Math.min(pageCount, value + 1))}>
              <ChevronRight className="size-4" />
            </Button>
          </div>
          {!isLoading && items.length === 0 ? <div className="px-6 py-14 text-center text-sm text-stone-500">没有找到图片</div> : null}
        </CardContent>
      </Card>
      <ImageLightbox
        images={lightboxImages}
        currentIndex={lightboxIndex}
        open={lightboxOpen}
        onOpenChange={setLightboxOpen}
        onIndexChange={setLightboxIndex}
      />
      <Dialog open={clearAllOpen} onOpenChange={(open) => (!open ? setClearAllOpen(false) : null)}>
        <DialogContent showCloseButton={false} className="rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>清空图片缓存</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              将删除服务器上的生成图、资产图和任务输入缓存，无法从本页恢复。账号、日志和任务记录不会删除。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="pt-2">
            <Button
              variant="secondary"
              className="h-10 rounded-xl bg-stone-100 px-5 text-stone-700 hover:bg-stone-200"
              onClick={() => setClearAllOpen(false)}
              disabled={isCleaning}
            >
              取消
            </Button>
            <Button
              className="h-10 rounded-xl bg-rose-600 px-5 text-white hover:bg-rose-500"
              onClick={() => void handleCleanup(0)}
              disabled={isCleaning}
            >
              {isCleaning ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
              确认清空
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

export default function ImageManagerPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  if (isCheckingAuth || !session || session.role !== "admin") {
    return <div className="flex min-h-[40vh] items-center justify-center"><LoaderCircle className="size-5 animate-spin text-stone-400" /></div>;
  }
  return <ImageManagerContent />;
}
