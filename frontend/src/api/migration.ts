import {request} from './client';
import type {MigrationImport, MigrationVerify} from '../types/domain';

function authHeaders(): HeadersInit {
  const token = localStorage.getItem('chengwan-token');
  return token ? {Authorization: 'Bearer ' + token} : {};
}

export const migrationApi = {
  imports: () => request<MigrationImport[]>('/migration/imports'),
  importPackage: (file: File) => {
    const form = new FormData();
    form.append('file', file);
    // 文件上传不能走默认 JSON 客户端：让浏览器自行设置 multipart 边界
    return fetch('/api/migration/imports', {
      method: 'POST',
      headers: authHeaders(),
      body: form,
    }).then(async (response) => {
      if (!response.ok) {
        const body = await response.json().catch(() => ({detail: '迁入失败，请重试'}));
        throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail));
      }
      return response.json() as Promise<MigrationImport>;
    });
  },
  exportBundle: async (datasetId: number): Promise<{digest: string}> => {
    const response = await fetch(`/api/migration/exports/${datasetId}`, {
      method: 'POST',
      headers: authHeaders(),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({detail: '迁出失败，请重试'}));
      throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail));
    }
    const blob = await response.blob();
    const digest = response.headers.get('x-package-digest') || '';
    const disposition = response.headers.get('content-disposition') || '';
    const star = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
    const filename = star ? decodeURIComponent(star[1]) : `research-bundle-${datasetId}.zip`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
    return {digest};
  },
  verify: (id: number) => request<MigrationVerify>(`/migration/imports/${id}/verify`),
};
