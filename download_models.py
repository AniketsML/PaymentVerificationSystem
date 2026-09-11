import os
from huggingface_hub import hf_hub_download

def download_ppocr_v5():
    model_dir = os.path.join(os.path.dirname(__file__), "models", "ppocr_v5")
    os.makedirs(model_dir, exist_ok=True)
    
    print("Downloading PP-OCRv5 Det Model...")
    det_path = hf_hub_download(repo_id="monkt/paddleocr-onnx", filename="detection/v5/det.onnx", local_dir=model_dir, local_dir_use_symlinks=False)
    
    print("Downloading PP-OCRv5 Rec Model...")
    rec_path = hf_hub_download(repo_id="monkt/paddleocr-onnx", filename="languages/english/rec.onnx", local_dir=model_dir, local_dir_use_symlinks=False)
    
    print(f"Models downloaded successfully to: {model_dir}")
    print(f"Det: {det_path}")
    print(f"Rec: {rec_path}")

if __name__ == "__main__":
    download_ppocr_v5()
