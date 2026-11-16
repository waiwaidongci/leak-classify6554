from fastapi import APIRouter,Depends,UploadFile,File
from fastapi.responses import Response
from urllib.parse import quote
from sqlalchemy.orm import Session
from src.config.database import get_db
from src.controllers.dependencies import current_user,writers
from src.services.migration.export import export_dataset
from src.services.migration.import_service import import_bundle,list_imports,get_import
from src.services.migration.provenance import verify_imported_run
from src.models import TrainingRun
from src.utils.serialize import row

router=APIRouter()

@router.post('/migration/exports/{dataset_id}')
def create_export(dataset_id:int,db:Session=Depends(get_db),user=Depends(writers)):
 blob,filename,manifest=export_dataset(db,dataset_id,user)
 disposition="attachment; filename=research-bundle.zip; filename*=UTF-8''"+quote(filename)
 return Response(blob,media_type='application/zip',headers={
  'Content-Disposition':disposition,
  'X-Package-Digest':manifest['package_digest'],
  'X-Manifest-Digest':manifest['manifest_digest'],
 })

@router.get('/migration/imports')
def imports(db:Session=Depends(get_db),user=Depends(current_user)):
 return list_imports(db)

@router.post('/migration/imports',status_code=201)
async def create_import(file:UploadFile=File(...),user=Depends(writers)):
 raw=await file.read()
 return import_bundle(raw,user)

@router.get('/migration/imports/{import_id}/verify')
def verify(import_id:int,db:Session=Depends(get_db),user=Depends(current_user)):
 registration=get_import(db,import_id)
 origin=registration.summary or {}
 reports=[]
 for target_id in origin.get('run_id_map',{}).values():
  run=db.get(TrainingRun,target_id)
  if not run: continue
  reports.append(verify_imported_run(db,run,{
   'window_id_map':origin.get('window_id_map',{}),
   'window_count':origin.get('window_count'),
   'source_fingerprints':origin.get('source_fingerprints',{}),
  }))
 return {'import_id':import_id,'package_digest':registration.package_digest,
  'origin_station':registration.origin_station,'reports':reports,
  'all_ok':bool(reports) and all(r['ok'] for r in reports)}

@router.get('/migration/imports/{import_id}')
def detail(import_id:int,db:Session=Depends(get_db),user=Depends(current_user)):
 return row(get_import(db,import_id))
