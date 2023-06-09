import os
import shutil
import torch
import pandas as pd
from pydantic import BaseModel
from google.cloud import storage, bigquery, aiplatform


class McvModelModel(BaseModel):
    model_name: str
    latest_version: int
    default_version: int
    training_runs: list[dict] = []
    prediction_runs: list[dict] = []
    metric_summary: list[dict] = []
    tensorboards: list[dict] = []

class MvcServiceModel(BaseModel):
    datasets: list[str]
    models: dict[str, McvModelModel] # model_name: McvModelModel
    endpoints: dict[str, aiplatform.models.Endpoint] # model_name: Enpoint

    class Config:
        arbitrary_types_allowed = True


def storage_driver(func):
    def wrapped(*args, **kwargs):
        try:
            args[0].storage_client = storage.Client(project=args[0].project_id)
            result = func(*args, **kwargs)
        except Exception as e:
            raise e
        return result
    return wrapped


class ModelVersionController():
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.project_id: str = os.environ["PROJECT_ID"]
        self.region: str = os.environ["REGION"]
        self.storage_client: storage.Client = None
        # self.bq_client: bigquery.Client = None
        self.services: dict[str, MvcServiceModel] = {} # service name
        self.service_config = os.environ["SERVICES_CONFIGED"].split(",")

        for service_name in self.service_config:
            # prefetch datasets
            datasets = self.list_datasets(service_name=service_name, init=True)
            self.services[service_name] = MvcServiceModel(
                datasets=datasets,
                models={},
                endpoints={},
            )
            # prefetch models
            aiplatform.init(
                project=self.project_id,
                location=self.region,
                staging_bucket=f"gs://{service_name}",
            )
            for model in aiplatform.Model.list():
                if service_name in model.display_name:
                    file_name = model.display_name.split("/")[-1]
                    self.create_service_model(service_name=service_name, model_file_name=file_name, model=model)

            # prefetch endpoints
            for end in list(aiplatform.Endpoint.list()):
                end_model = end.list_models()
                if len(end_model) > 0:
                    # TODO - multi-model endpoint unavailable
                    model_name = end_model[0].display_name
                    if service_name in model_name:
                        self.services[service_name].endpoints[model_name] = end

    def gen_file_path(self, dataset_name: str, version: str, file_format: str):
        return f"{dataset_name}_{version}.{file_format}"
    
    def gen_dataset_storage_path(self, service_name: str, dataset_name: str, version: str, file_format: str):
        file_path = self.gen_file_path(dataset_name=dataset_name, version=version, file_format=file_format)
        return f"gs://{service_name}/{file_path}"
    
    def gen_gcs_file_path(self, service_name: str, dataset_name: str, version: str | None = None, file_format: str = "csv"):
        datasets = [d for d in self.services[service_name].datasets if dataset_name in d]

        if not version:
            curr_version = str(max([int(d.split(".")[0].split("_")[-1]) for d in datasets]))
            file_path = self.gen_dataset_storage_path(service_name=service_name, dataset_name=dataset_name, version=curr_version, file_format=file_format)
        else:
            file_path = self.gen_dataset_storage_path(service_name=service_name, dataset_name=dataset_name, version=version, file_format=file_format)

        return file_path

    @storage_driver
    def create_dataset(self, df: pd.DataFrame, service_name: str, dataset_name: str, new_version: bool = False, file_format: str = "csv"):
        datasets = self.list_datasets(service_name)
        d_avail = [d for d in datasets if dataset_name in d]

        overwrite = False
        if len(d_avail) < 1:
            version = "1"
        else:
            versions = [int(d.split(".")[0].split("_")[-1]) for d in d_avail]
            latest_version = max(versions)
            if new_version:
                version = str(latest_version + 1)
            else:
                overwrite = True
                version = str(latest_version)

        if overwrite:
            self._delete_datasets(service_name=service_name, dataset_name=dataset_name, version=version, file_format=file_format)

        storage_path = self.gen_dataset_storage_path(service_name=service_name, dataset_name=dataset_name, version=version, file_format=file_format)     
        df.to_csv(storage_path, index=False)

        return self.list_datasets(service_name)

    @storage_driver
    def list_datasets(self, service_name: str, init=False):
        if not init:
            assert service_name in self.services, "service name not in CONFIG"

        bucket = self.storage_client.bucket(service_name)
        if not bucket.exists():
            bucket.create()
            return []
        blobs = bucket.list_blobs()
        datasets = [b.name for b in blobs if "vertex_ai_auto_staging" not in b.name]

        if not init:
            self.services[service_name].datasets = datasets

        return datasets

    def get_dataset(self, service_name: str, dataset_name: str, version: str | None = None, file_format: str = "csv"):
        datasets = [d for d in self.services[service_name].datasets if dataset_name in d]
        file_path = self.gen_gcs_file_path(service_name=service_name, dataset_name=dataset_name, version=version, file_format=file_format)
        for d in datasets:
            if d in file_path:
                df = pd.read_csv(file_path)
                if "TIMESTAMP" in df.columns:
                    df["TIMESTAMP"] = pd.to_datetime(df["TIMESTAMP"])
                return df
        return pd.DataFrame()

    @storage_driver
    def _delete_datasets(self, service_name: str, dataset_name: str, version: str | None = None, file_format: str = "csv"):
        bucket = self.storage_client.bucket(service_name)
        blobs = bucket.list_blobs()
        for blob in blobs:
            if version is not None:
                file_name = self.gen_file_path(dataset_name=dataset_name, version=version, file_format=file_format)
                if file_name == blob.name:
                    blob.delete()
            else:
                if dataset_name in blob.name:
                    blob.delete()

    def create_vertex_dataset(self, service_name: str, dataset_name: str, version: str | None = None):
        file_path = self.gen_gcs_file_path(service_name=service_name, dataset_name=dataset_name, version=version)

        aiplatform.init(
            project=self.project_id,
            location=self.region,
            staging_bucket=service_name,
        )

        for d in aiplatform.TimeSeriesDataset.list():
            if d.display_name == dataset_name:
                d.delete()

        aiplatform.TimeSeriesDataset.create(
            display_name=dataset_name,
            gcs_source=[file_path],
        )

    def _model_storage_path(self, service_name: str, file_name: str) -> str:
        return f"{service_name}/{file_name}"

    def _model_storage_name(self, service_name: str, file_name: str) -> str:
        return f"{service_name}-{file_name}"

    def create_service_model(self, service_name: str, model_file_name: str, model) -> bool:
        try:
            default_version = int([m.version_id for m in model.versioning_registry.list_versions() if "default" in m.version_aliases][0])
        except:
            default_version = 0

        model = McvModelModel(
            model_name=model_file_name,
            latest_version=model.version_id,
            default_version=default_version,
        )
        self.services[service_name].models[model_file_name] = model
        return True

    # VERTEX MODELS
    @storage_driver
    def save_model(self, model_object, service_name: str, model_file_name: str, garbage_collect: bool = True): 
        registry_uri = self._model_storage_path(service_name=service_name, file_name=model_file_name)
        local_path = "".join(registry_uri.split("/"))
        
        os.makedirs(registry_uri, exist_ok=True)

        torch.save(model_object.state_dict(), os.path.join(registry_uri, 'model.pt'))
        aiplatform.init(
            project=self.project_id,
            location=self.region,
            staging_bucket=f"gs://{service_name}",
        )

        models = aiplatform.Model.list(filter=(f"display_name={registry_uri}"))
        if len(models) == 0:
            model_uploaded = aiplatform.Model.upload(
                display_name=registry_uri,
                artifact_uri=registry_uri,
                # container_predict_route="/predict",
                # container_health_route="/health",
                serving_container_image_uri=os.environ["MODEL_PREDICT_CONTAINER_URI"],
                is_default_version=True,
                # version_description="This is the first version of the model",
            )

        else:
            parent_model = models[0].resource_name
            model_uploaded = aiplatform.Model.upload(
                display_name=registry_uri,
                artifact_uri=registry_uri,
                # container_predict_route="/predict",
                # container_health_route="/health",
                serving_container_image_uri=os.environ["MODEL_PREDICT_CONTAINER_URI"],
                is_default_version=False,
                parent_model=parent_model,
                # version_description="This is the first version of the model",
            )

        model_uploaded.wait()

        if garbage_collect:
            shutil.rmtree(local_path)

        if model_file_name not in self.services[service_name].models:
            return self.create_service_model(service_name=service_name, model_file_name=model_file_name, model=model_uploaded)

        print("end")
        self.services[service_name].models[model_file_name].latest_version = model_uploaded.version_id
        return True

    @storage_driver
    def load_model(self, service_name: str, model_file_name: str, latest_dev_version: bool = False):
        '''
        NOTE: when using this function you will need to define
        model = mvc.load_model(...)
        ModelArchitecture.load_state_dict(model)
        '''
        aiplatform.init(
            project=self.project_id,
            location=self.region,
            staging_bucket=f"gs://{service_name}",
        )

        registry_uri = self._model_storage_path(service_name=service_name, file_name=model_file_name)

        models = aiplatform.Model.list(filter=(f"display_name={registry_uri}"))
        
        if len(models) > 0:
            # model = models[0]
            if latest_dev_version:
                model = models[-1]
            else:
                model = [m for m in models if "default" in m.version_aliases][0]
            model.load_state_dict(torch.load(model.uri))
            return torch.load(model.uri)
        
        return False

    def predict_endpoint(self, service_name: str, model_name: str, x_instance: pd.DataFrame | None = None, x_batch: list[pd.DataFrame] | None = None):
        assert x_instance is not None or x_batch is not None, "Must provide either x_instance or x_batch"
        end_name = f"{service_name}/{model_name}"
        if end_name in self.services[service_name].endpoints:
            try:
                if x_instance is not None:
                    predictions = self.services[service_name].endpoints[end_name].predict(instances=x_instance)
                else:
                    predictions = self.services[service_name].endpoints[end_name].batch_predict(
                        instances=x_batch, parameters={"confidence_threshold": 0.5}
                    )
                return predictions
            except Exception as e:
                print(f"PREDICTION ERROR: {e}")
                print(f"model service name {model_name}")
                return None
        else:
            print(f"endpoint for model {model_name} for service {service_name} not found in endpoints")
            return None