from fastapi import FastAPI, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
import cv2
import mediapipe as mp
import numpy as np
import tensorflow as tf
from tensorflow.keras.applications import Xception
from tensorflow.keras.layers import Layer, GlobalAveragePooling2D, Dense, Dropout, Input, Multiply, Reshape, MaxPooling2D, Conv2D, Add, Activation, Concatenate, Lambda
from tensorflow.keras.models import Model
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.regularizers import l2
from tensorflow.keras.preprocessing import image
from fastapi.responses import HTMLResponse
from fastapi import FastAPI, UploadFile, File
import base64
import random
import json

# json檔路徑
json_path = '/home/wei_jai/test.json'

# 模型自訂層
# XceptionLayer 
class XceptionLayer(Layer):
    def __init__(self, **kwargs):
        super(XceptionLayer, self).__init__(**kwargs)
        self.xception = Xception(weights='imagenet', include_top=False)

    def call(self, inputs, input_shape=(100, 100, 3)):
        return self.xception(inputs)

    def get_config(self):
        config = super().get_config()  
        return config

# CBAMLayer 
class CBAMLayer(Layer):
    def __init__(self, reduction_ratio=16, **kwargs):
        super(CBAMLayer, self).__init__(**kwargs)
        self.reduction_ratio = reduction_ratio

    def build(self, input_shape):
        # 通道注意力
        self.channel_avg_pool = GlobalAveragePooling2D()
        self.channel_max_pool = MaxPooling2D()
        self.channel_dense_1 = Dense(input_shape[-1] // self.reduction_ratio, activation='relu')
        self.channel_dense_2 = Dense(input_shape[-1], activation='sigmoid')

        # 空间注意力
        self.spatial_conv = Conv2D(1, kernel_size=(7, 7), padding='same', activation='sigmoid')

    def call(self, inputs):
        # 通道注意力
        channel_avg_pool = self.channel_avg_pool(inputs)
        channel_max_pool = self.channel_max_pool(inputs)
        channel_avg_pool = Reshape((1, 1, -1))(channel_avg_pool)
        channel_max_pool = Reshape((1, 1, -1))(channel_max_pool)
        channel_avg_pool = self.channel_dense_1(channel_avg_pool)
        channel_max_pool = self.channel_dense_1(channel_max_pool)
        channel_avg_pool = self.channel_dense_2(channel_avg_pool)
        channel_max_pool = self.channel_dense_2(channel_max_pool)
        channel_attention = Add()([channel_avg_pool, channel_max_pool])
        channel_attention = Multiply()([inputs, channel_attention])

        # 空间注意力
        avg_pool = Lambda(lambda x: tf.reduce_mean(x, axis=-1, keepdims=True))(channel_attention)
        max_pool = Lambda(lambda x: tf.reduce_max(x, axis=-1, keepdims=True))(channel_attention)
        concat = Concatenate(axis=-1)([avg_pool, max_pool])
        spatial_attention = self.spatial_conv(concat)
        spatial_attention = Multiply()([channel_attention, spatial_attention])

        return spatial_attention

    def get_config(self):
        config = super().get_config()
        config.update({'reduction_ratio': self.reduction_ratio})
        return config
    
app = FastAPI()
# 添加 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 或者设置为你允许的域名，如 ["https://yourwebsite.com"]
    allow_credentials=True,
    allow_methods=["POST", "GET"],  # 添加GET方法
    allow_headers=["*"],
)

# 载入模型
# 指定模型路徑
MODEL_PATHS = {
    "Model 1": "/home/wei_jai/model.keras",
    "Model 2": "/home/wei_jai/dog_cat.keras",
    "Model 3": "/home/wei_jai/model.keras",
}

# 處理從網頁收到的模型類別資訊
def load_model_by_label(label):
    if label in MODEL_PATHS:
        return tf.keras.models.load_model(MODEL_PATHS[label], custom_objects={'XceptionLayer': XceptionLayer, 'CBAMLayer': CBAMLayer})
    else:
        raise ValueError(f"Invalid model label: {label}")

# 初始化MediaPipe的面部檢測器和面部關鍵點檢測器
mp_face_detection = mp.solutions.face_detection
mp_drawing = mp.solutions.drawing_utils
mp_face_mesh = mp.solutions.face_mesh
face_mesh = mp_face_mesh.FaceMesh(static_image_mode=True, refine_landmarks=True, max_num_faces=1, min_detection_confidence=0.5)

# 定義全局變量 original_img
original_img = None

# 初始化MediaPipe的面部檢測器和面部關鍵點檢測器
mp_face_mesh = mp.solutions.face_mesh
face_mesh = mp_face_mesh.FaceMesh(static_image_mode=True, max_num_faces=1, min_detection_confidence=0.5)


# 定義特徵點連線顏色
def connect_points(image, coordinates, color):
    for i in range(len(coordinates) - 1):
        cv2.line(image, coordinates[i], coordinates[i + 1], color, 2)

def detect_face_landmarks(image, results, feature_points_keys):
    # 从JSON文件加载自定义特征点信息和所占比例
    with open(json_path, 'r', encoding='utf-8') as file:
        data = json.load(file)

    # 从mu_to_na中获取颜色映射
    mu_color_mapping = {mu["mu_no"]: mu["mu_color"] for mu in data["mu_to_na"]}

    if results.multi_face_landmarks:
        for feature_points_key in feature_points_keys:
            # 创建列表存储计算后的坐标
            final_coordinates = []

            # 检查是否存在特征点数据
            if feature_points_key not in data:
                print(f"No feature points found for key: {feature_points_key}")
                continue  # 如果没有特征点，跳过当前键

            for item in data[feature_points_key]:
                # 确保包含比例因子和特征点编号
                if "p" not in item or "v" not in item:
                    print(f"Missing data for item: {item}")
                    continue  # 如果数据不完整，跳过该项

                scale_factors = [float(factor) for factor in item["p"].split()]  # 比例因子列表
                feature_point_indices = [int(idx) for idx in item["v"].split()]  # 特征点编号列表

                # 初始化变量
                summed_x = 0
                summed_y = 0
                summed_z = 0

                # 将指定特征点之相对坐标乘以比例因子后计算出所需肌肉部位坐标
                for idx, factor in zip(feature_point_indices, scale_factors):
                    # 确保索引在有效范围内
                    if idx < len(results.multi_face_landmarks[0].landmark):
                        landmark = results.multi_face_landmarks[0].landmark[idx]
                        x, y, z = landmark.x, landmark.y, landmark.z
                        x_image = int(x * image.shape[1] * factor)  # 转换为图像坐标系的 x 坐标并乘上比例因子
                        y_image = int(y * image.shape[0] * factor)  # 转换为图像坐标系的 y 坐标并乘上比例因子
                        z_image = int(z * factor)  # z 坐标乘上比例因子
                        summed_x += x_image
                        summed_y += y_image
                        summed_z += z_image

                # 将最终坐标保存到列表中
                if summed_x != 0 and summed_y != 0:  # 确保坐标有效
                    final_coordinates.append((summed_x, summed_y))

            # 使列表中的最后一个特征点与第一个特征点重合
            if final_coordinates:
                final_coordinates.append(final_coordinates[0])

                # 根据feature_points_key获取颜色
                color = mu_color_mapping.get(feature_points_key, "#ffffff")  # 默认颜色为白色

                # 转换颜色为元组格式（B, G, R）
                color_tuple = tuple(int(color[i:i+2], 16) for i in (1, 3, 5))[::-1]

                # 调用连线函数
                connect_points(image, final_coordinates, color_tuple)

    return image



@app.post("/emotion_recognition")
async def emotion_recognition(file: UploadFile = File(...), model_label: str = Form(...)):
    global json_path  # 使用全局變量 json_path
    # 讀取模型類別，失敗則返回錯誤訊息。
    try:
        model = load_model_by_label(model_label)
    except ValueError as e:
        return {"error": str(e)}
        
    contents = await file.read()
    nparr = np.frombuffer(contents, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    original_img = img.copy()  # 備份原始圖像
    
    # 進行面部網格檢測
    results = face_mesh.process(original_img)
    
    # 如果未偵測到任何人臉，返回訊息
    if not results.multi_face_landmarks:
        return {"result": "未偵測到人臉", "muresult": ""}
    
    # 如果有偵測到人臉，執行表情預測
    else:
        # 調整圖像大小以匹配模型輸入大小
        img = cv2.resize(img, (100, 100))
        img_array = np.expand_dims(img, axis=0)  # 添加批次維度
        img_array = img_array / 255.0  # 正規化像素值
        
        # 進行預測
        predictions = model.predict(img_array)
        
        # 解碼預測結果
        predicted_class = np.argmax(predictions)
        
        # 讀取 JSON 資料
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 查詢表情對應的AU和MU 
        exp_data = None
        for exp in data['exp_to_au']:
            if str(predicted_class) == str(exp.get('exp_num')):  # 對應表情代碼
                exp_data = exp
                break
        
        if exp_data:
            emotion_result = exp_data.get('exp', '未知表情')  # 取得表情名稱
            au_list = exp_data.get('au_no', '').split()  # 將 AU 字符串分割為列表
            
            mu_list = []
            mu_names = []
            mu_colors = []
            for au in au_list:
                for au_data in data['au_to_mu']:
                    if au_data['au_no'] == au:  # 查找對應的 AU
                        mus = au_data['mu_no'].split()  # 將 mu_no 分割為多個肌肉編號
                        mu_list.extend(mus)
                        for mu in mus:
                            for mu_data in data['mu_to_na']:
                                if mu_data['mu_no'] == mu:  # 查找對應的 MU
                                    mu_names.append(mu_data['mu_na'])
                                    mu_colors.append(mu_data['mu_color'])
                                    break
            
            # 使用查詢到的MU執行檢測
            processed_image = detect_face_landmarks(original_img, results, mu_list)
            mu_result = ', '.join(mu_names)  # 將MU名稱列表拼接成字串
            mu_color_result = ', '.join(mu_colors)  # 返回對應的顏色
            
        else:
            emotion_result = "未知表情"
            processed_image = original_img
            mu_result = ""
            mu_color_result = ""
        
        # 將處理後的圖像編碼為Base64格式
        _, img_encoded = cv2.imencode('.jpg', processed_image)
        img_base64 = base64.b64encode(img_encoded).decode()  # 獲取圖像的Base64編碼
        
        # 返回圖像和結果
        return {"image": img_base64, "result": emotion_result, "muresult": mu_result, "mu_colors": mu_color_result}
        
@app.get("/web1", response_class=HTMLResponse)
async def web1():
    return """   
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>主頁</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons/font/bootstrap-icons.css" rel="stylesheet"> <!-- Bootstrap 圖標CSS連結 -->
  <style>
    body {
      background-color:#F4F4F4;
    }

    /* 容器_導覽列樣式 */
    .navbar-container {
      display: flex;
      align-items: center; /* 水平置中容器內元素 */
      justify-content: space-between; /* 元素分別靠最左和最右端對齊 */
      border-bottom: 1px solid #ccc; /* 底線 */
      padding-bottom: 10px; /* 容器內元素與容器間的邊距 */
      margin-bottom: 20px; /* 容器的外邊距 */
    }
    
     /* 導覽列樣式 */
    .navbar-btn {
      display: flex;
      justify-content: center;
      align-items: center;
      width: 45px;
      height: 45px;
      border-radius: 50%;
      border: none;
      cursor: pointer;
      transition: background-color 0.3s ease;
      background-color: #F4F4F4;
    }

    /* 導覽列-按鈕觸碰變色 */
    .navbar-btn:hover {
      background-color: #f0f0f0;
    }

    /* 導覽列-按鈕點擊變色 */
    .navbar-btn:active {
      background-color: #808080;
    }

    /* 左側選單樣式 */
    #sidebar {
      position: fixed; /* 將選單設置為固定位置 */
      top: 0;
      left: -400px; /* 將選單放置頁面外 */
      width: 200px;
      height: 100vh;
      background-color: #ede9e8;
      transition: left 0.3s ease; /* 實現選單滑入效果 */
      padding: 20px;
      box-shadow: 0px 0px 10px 0px rgba(0,0,0,0.3);
      z-index: 2;
    }

    /* 左側選單-選項容器樣式 */
    .sidebar-selection-container {
      border-bottom: 1px solid #ccc;
      padding-bottom: 10px;
      margin-bottom: 20px;
    }

    /* 左側選單-選項文字樣式 */
    .sidebar-btn-text {
      text-align: left;
      font-size: 15px;
    }

    /* 左側選單-選項按鈕樣式 */
    .sidebar-btn {
      display: flex;
      flex-direction: column;
      align-items: center;
      flex-direction: row;
      width: 100%;
      height: 40px;
      border: none;
      border-radius: 12px;
      background-color: #ede9e8;
      color: #9e9092;
      gap: 10px;
      margin-bottom: 10px;
      cursor: pointer;
      transition: background-color 0.3s ease, color 0.3s ease;     
    }

    /* 左側選單-選項按鈕觸碰變色 */
    .sidebar-btn:hover {
      background-color: #ffffff;
    }

    /* 左側選單-選項按鈕點擊變色 */
    .sidebar-btn:active {
      background-color: #808080;
      color: #ffffff;
    }
  </style>
</head>

<body>
  <!-- 導覽列 -->
  <div class="navbar-container">
    <!-- 按鈕_顯示左側選單 -->
    <button class="navbar-btn" id="sidebarBtn"><i class="bi bi-list" style="transform: scale(1.5);"></i></button>     
    <!-- 按鈕_顯示設定選單 -->
    <button class="navbar-btn" id="settingBtn"><i class="bi bi-gear" style="transform: scale(1.5);"></i></button>   
  </div>

  <!-- 左側選單 -->
  <div id="sidebar">
    <div class="sidebar-selection-container">
      <!-- 左側選單選項 -->
      <button class="sidebar-btn" id="homeBtn"><i class="bi bi-house" style="transform: scale(1.5)"></i>
        <div class="sidebar-btn-text">主頁</div>
      </button>
      <button class="sidebar-btn" id="emotionBtn"><i class="bi bi-clipboard-data-fill" style="transform: scale(1.5)"></i>
        <div class="sidebar-btn-text">臉部肌肉分析</div>
      </button> 
    </div>
  </div>

  <script>  
    document.addEventListener('DOMContentLoaded', function() {
      const sidebarBtn = document.getElementById('sidebarBtn');
      const settingBtn = document.getElementById('settingBtn');
      const sidebar = document.getElementById('sidebar');
      const homeBtn = document.getElementById('homeBtn');
      const emotionBtn = document.getElementById('emotionBtn');
      
      // 當點擊sidebar按钮
      sidebarBtn.addEventListener('click', function(event) {
        sidebar.style.left = '0'; // 顯示左側選單
        event.stopPropagation(); // 阻止事件冒泡
      });

      // 當點擊setting按钮
      settingBtn.addEventListener('click', function(event) {
        event.stopPropagation(); // 阻止事件冒泡
      });

      // 當點擊空白處
      document.addEventListener('click', function(event) {
        const target = event.target;
        if (!sidebar.contains(target) && target !== sidebarBtn) {
          sidebar.style.left = '-400px'; // 隱藏左側選單
        }
      });

      // 點擊"主頁"
      homeBtn.addEventListener('click', function() {
        window.location.href = 'https://1e1c-210-59-96-137.ngrok-free.app/web1'; 
      });

      // 點擊"表情辨識 & 顯示動作肌群"
      emotionBtn.addEventListener('click', function() {
        window.location.href = 'https://1e1c-210-59-96-137.ngrok-free.app/web2'; 
      });
    });
  </script>
</body>
</html>
"""

@app.get("/web2", response_class=HTMLResponse)
async def web2():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="icon" href="https://i.imgur.com/SzojS5O.png" sizes="32x32">
    <title>表情辨識與肌肉分析</title>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/bootstrap-icons/1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
   <style>
    body {       
        
        background-color: #f0f0f0;
        overflow: auto;
    }

    /* 頁面尺寸變換時,改變排版 */
    @media (max-width: 768px) {
        .container {
            flex-direction: column;
            gap: 30px;
            overflow-y: auto;
        }
    }

    /* 容器放置分析和回傳類功能 */
    .container {       
        display: flex;
        justify-content: center;
        align-items: center;
        gap: 30px;
        margin: 0;
        padding: 0;
        min-height: 100vh;
        background-color: #f0f0f0;
        overflow: auto;
    }
    
    .text {
	display: flex; /* 使用 flexbox 讓內容保持在一行 */
        align-items: flex-start; /* 保持文字和換行內容的頂部對齊 */
        margin-bottom: 5px;
        font-size: 20px;
    }

     /* 容器_導覽列樣式 */
    .navbar-container {
      display: flex;
      align-items: center; /* 水平置中容器內元素 */
      justify-content: space-between; /* 元素分別靠最左和最右端對齊 */
      border-bottom: 1px solid #ccc; /* 底線 */
      padding-bottom: 10px; /* 容器內元素與容器間的邊距 */
      margin-bottom: 20px; /* 容器的外邊距 */      
    }
    
     /* 導覽列樣式 */
    .navbar-btn {
      display: flex;
      justify-content: center;
      align-items: center;
      width: 45px;
      height: 45px;
      border-radius: 50%;
      border: none;
      background-color: transparent;
      cursor: pointer;
      transition: background-color 0.3s ease;
    }

    /* 導覽列-按鈕觸碰變色 */
    .navbar-btn:hover {
      background-color: #f0f0f0;
    }

    /* 導覽列-按鈕點擊變色 */
    .navbar-btn:active {
      background-color: #808080;
    }

    /* 左側選單樣式 */
    #sidebar {
      position: fixed; /* 將選單設置為固定位置 */
      top: 0;
      left: -400px; /* 將選單放置頁面外 */
      width: 200px;
      height: 100vh;
      background-color: #ede9e8;
      transition: left 0.3s ease; /* 實現選單滑入效果 */
      padding: 20px;
      box-shadow: 0px 0px 10px 0px rgba(0,0,0,0.3);
      z-index: 2;
    }

    /* 左側選單-選項容器樣式 */
    .sidebar-selection-container {
      border-bottom: 1px solid #ccc;
      padding-bottom: 10px;
      margin-bottom: 20px;
    }

    /* 左側選單-選項文字樣式 */
    .sidebar-btn-text {
      text-align: left;
      font-size: 15px;
    }

    /* 左側選單-選項按鈕樣式 */
    .sidebar-btn {
      display: flex;
      flex-direction: column;
      align-items: center;
      flex-direction: row;
      width: 100%;
      height: 40px;
      border: none;
      border-radius: 12px;
      background-color: #ede9e8;
      color: #9e9092;
      gap: 10px;
      margin-bottom: 10px;
      cursor: pointer;
      transition: background-color 0.3s ease, color 0.3s ease;     
    }

    /* 左側選單-選項按鈕觸碰變色 */
    .sidebar-btn:hover {
      background-color: #ffffff;
    }

    /* 左側選單-選項按鈕點擊變色 */
    .sidebar-btn:active {
      background-color: #808080;
      color: #ffffff;
    }

    /* 容器_一般 */
    .norm-container {
        display: flex;
        flex-direction: column;
        justify-content: flex-start;
        align-items: flex-start;
        max-width: 500px;
        width: 100%;
        gap: 15px;
        border: 2px solid #aaaaaa;
        font-size: 20px;
        font-weight: bold;
        color: #7F7F7F;
        padding: 15px;
        position: relative;
        box-sizing: border-box;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.2);
        border-radius: 10px;
    }

    /* 容器_添加樣本按鈕 */
    .sample-btn-container {
        display: flex;
        flex-direction: row;
        justify-content: flex-start;
        align-items: flex-start;
        max-width: 500px;
        width: 100%;
        gap: 15px;
        box-sizing: border-box;
    }

    /* 容器_添加樣本 */
    .add-sample-container {
        display: none;
        flex-direction: column;
        justify-content: center;
        align-items: center;
        gap: 10px;
        width: 100%;
        max-width: 700px;
        padding-top: 20px;
        padding-bottom: 20px;
        box-sizing: border-box;
    }

    /* 添加樣本按鈕 */
    .add-sample-btn {
        display: flex;
        flex-direction: column;
        justify-content: center;
        align-items: center;
        gap: 5px;
        padding: 12px 20px;
        font-size: 16px;
        cursor: pointer;
        background-color: #8abeff;
        color: white;
        border: none;
        border-radius: 5px;
    }

    /* 空方框 */
    .box {
        position: relative;
        max-width: 400px;
        width: 100%;
        aspect-ratio: 1 / 1;
        background-color: transparent;
        border: 3px solid black;
        box-sizing: border-box;
        border-radius: 5px;
        overflow: hidden; /* 確保內容不會超出容器 */
    }

    /* 空方框圖像設定 */
    .box img {
        position: absolute;
        width: 100%;
        height: 100%;
        object-fit: cover; /* 填满容器并保持纵横比 */
    }

    /* 視訊框內的圓框濾鏡 */
    .facefilter {
        position: absolute;
        top: 50%;
        left: 50%;
        transform: translate(-50%, -50%);
        width: 80%;
        height: 95%;
        background-color: transparent;
        border: 5px solid black;
        border-radius: 50%;
    }

    /* 按鈕普通 */
    .norm-button {
        margin-top: 10px;
        padding: 10px 20px;
        font-size: 16px;
        cursor: pointer;
        background-color: #4CAF50;
        color: white;
        border: none;
        border-radius: 5px;
    }

    /* 按鈕-關閉OR返回 */
    .close-button {
 	display: flex;
        position: absolute;
        top: 90px;
        right: 20px;    
        background-color: transparent;
        border: none;
        cursor: pointer;   
    }

    /* 模型設定按鈕 */
    .model-settings-btn {
        position: absolute;
        bottom: 10px;
        right: 10px;
        background-color: transparent;
        border: none;
        cursor: pointer;
        font-size: 24px;
        display: none;
    }

    /* 容器-模型設定 */
    .model-settings-container {
        width: 70%;
    }

    /* 模型設定-內容 */
    .model-settings-menu {
        display: none;
        position: absolute;
        bottom: -60px;
        right: -190px;
        background-color: white;
        border: 1px solid #ccc;
        border-radius: 5px;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
        width: 200px;
        padding: 10px;
        box-sizing: border-box;
        z-index: 10;
    }

    /* 模型設定內容-顯示 */
    .model-settings-menu.show{
        display: flex;
        flex-direction: row;
    }

    /* 模型設定-內容-標題 */
    .model-settings-menu .menu-header {
        font-size: 15px;
        margin-bottom: 10px;
        font-weight: bold;
    }

    /* 模型設定-內容-選擇項目 */
    .model-settings-menu select {
        width: 100%;
        padding: 8px;
        border: 1px solid #ccc;
        border-radius: 5px;
        font-size: 16px;
    }

    /* 預留按鈕 */
    .reserved-menu-btn {
        position: absolute;
        top: 10px;
        right: 10px;
        background-color: transparent;
        border: none;
        cursor: pointer;
        font-size: 24px;
    }

    /* 預留按鈕-內容 */
    .reserved-menu {
        display: none;
        position: absolute;
        top: 40px;
        right: -140px;
        background-color: white;
        border: 1px solid #ccc;
        border-radius: 5px;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
        width: 150px;
        height: auto;
        padding: 0;
        box-sizing: border-box;
        overflow: hidden;
        flex-direction: column;
        z-index: 10;
    }

    /* 預留按鈕-顯示 */
    .reserved-menu.show {
        display: flex;
    }

    /* 預留按鈕-內容-選項 */
    .reserved-menu button {
        flex: 1;
        background-color: transparent;
        border: none;
        font-weight: bold;
        color: #7F7F7F;
        padding: 8px;
        margin: 0;
        text-align: left;
        cursor: pointer;
    }

    /* 預留按鈕-內容-選項觸碰變色 */
    .reserved-menu button:hover {
        background-color: #f0f0f0;
    }

    /* 圖標-內容提示 */
    .tip-icon {
        display: flex;
        background-color: transparent;
        color: #6e6e6e;
        cursor: pointer;
        margin-top: 20px;
        margin-left: 25px;
    }

    /* 背景-內容提示 */
    .tip {
        position: absolute;
        background-color: #333;
        color: #fff;
        text-align: center;
        padding: 10px;
        border-radius: 5px;
        width: 150px;
        z-index: 1;
        font-size: 18px;
        display: none;
        word-wrap: break-word;
        white-space: normal;
        word-break: keep-all;
    }

    /* 攝像頭影像 */
    #video {
        width: 100%;
        height: 100%;
        transform: scaleX(-1);
        filter: brightness(100%); /* 调整亮度的滤镜，数值可以根据需求调整 */
        object-fit: cover; /* 填满容器并保持纵横比 */
    }

    /* 圖像上傳預覽 */
    #UploadSamplePreview img {
        width: 100%;
        height: 100%;
        object-fit: contain; /* 確保圖片自動縮放填滿容器，且不會變形 */
    }

    /* 容器_回傳結果 */
    .result-container {
        display: flex;
        flex-direction: column;
        justify-content: center;
        align-items: center;
        gap: 20px;
        width: 100%;
        max-width: 700px;
        padding-top: 10px;
        padding-bottom: 10px;
        box-sizing: border-box;
    }

    /* 回傳結果-圖像 */
    #result-img {
        transform: scaleX(-1);
        filter: brightness(100%); /* 调整亮度的滤镜，数值可以根据需求调整 */
    }

    /* 容器-回傳結果-情緒 */
    #emotion-container {
        display: flex;
        justify-content: center;
        flex-wrap: wrap; /* 允許換行 */
        gap: 15px;
        width: 300px; /* 控制容器寬度 */
    }

    /* 回傳結果-情緒 */
    .expmessage {
        display: flex;
        align-items: center;
        background-color: white;
        padding: 10px;
        border-radius: 8px;
        box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
        opacity: 0;
        transform: translateY(100%);
        transition: all 1s ease-out; /* 縮短過渡動畫至 0.2 秒 */
    }

    /* 回傳結果-情緒-訊息滑入效果 */
    .expmessage.visible {
        opacity: 1;
        transform: translateY(0);
    }

    /* 回傳結果-情緒-圖標 */
    .expicon {        
        margin-right: 10px;
    }

    /* 容器-回傳結果-肌肉 */
    #mu-container {
        display: flex;
        flex-wrap: wrap; /* 允許換行 */
        gap: 15px;
        width: 300px; /* 控制容器寬度 */
    }

    /* 回傳結果-肌肉 */
    .mumessage {
        display: flex;
        align-items: center;
        background-color: white;
        padding: 10px;
        border-radius: 8px;
        box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
        opacity: 0;
        transform: translateY(100%);
        transition: all 1s ease-out; /* 縮短過渡動畫至 0.2 秒 */
        width: calc(40%); /* 每條訊息寬度為 25% 減去間隔，以便顯示四條訊息一行 */
    }

    /* 回傳結果-肌肉-訊息滑入效果 */
    .mumessage.visible {
        opacity: 1;
        transform: translateY(0);
    }

    /* 回傳結果-肌肉-圖標 */
    .muicon {
        width: 25px;
        height: 25px;
        border-radius: 50%;
        margin-right: 10px;
    }


</style>

</head>
<body>
    <!-- 導覽列 -->
  <div class="navbar-container">
    <!-- 按鈕_顯示左側選單 -->
    <button class="navbar-btn" id="sidebarBtn"><i class="bi bi-list" style="transform: scale(1.5);"></i></button>     
    <!-- 按鈕_顯示設定選單 -->
    <button class="navbar-btn" id="settingBtn"><i class="bi bi-gear" style="transform: scale(1.5);"></i></button>   
  </div>

  <!-- 左側選單 -->
  <div id="sidebar">
    <div class="sidebar-selection-container">
      <!-- 左側選單選項 -->
      <button class="sidebar-btn" id="homeBtn"><i class="bi bi-house" style="transform: scale(1.5)"></i>
        <div class="sidebar-btn-text">主頁</div>
      </button>
      <button class="sidebar-btn" id="emotionBtn"><i class="bi bi-clipboard-data-fill" style="transform: scale(1.5)"></i>
        <div class="sidebar-btn-text">臉部肌肉分析</div>
      </button> 
    </div>
  </div>

<!-- 分析和回傳類功能容器 -->
<div class="container" id="container">
    <!-- 新增圖片樣本容器 -->
    <div class="norm-container" id="sample-container">
        新增圖片樣本:
        <div style="border-bottom: 1px solid #aaaaaa; width: 100%;"></div>

        <!-- 預留按鈕和選單 -->
        <button class="reserved-menu-btn" id="reserved-menu-btn">
            <i class="bi bi-three-dots-vertical"></i>
        </button>
	<div class="reserved-menu" id="reserved-menu">
            <button>選項 1</button>
            <button>選項 2</button>
            <button>選項 3</button>
            <button>選項 4</button>
            <button>選項 5</button>
        </div>

        <!-- 模型設定按鈕和選單 -->
        <button class="model-settings-btn" id="model-settings-btn">
            <i class="bi bi-gear"></i>
        </button>
        <div class="model-settings-menu" id="model-settings-menu">
            <div class="model-settings-container">
                <div class="menu-header">選擇模型</div>
                <select id="modelSelect">
                    <option>Model 1</option>
                    <option>Model 2</option>
                    <option>Model 3</option>
                </select>
            </div>
            <!-- 模型設定說明 -->
            <div class="tip-icon" id="model-settings-tip-icon">
                <i class="bi bi-question-diamond"></i>
                <div class="tip" id="model-settings-tip">
                    可於選單中切換用於辨識的模型種類。
                </div>
            </div>
        </div>

        <!-- 新增圖片樣本選項-攝像頭和上傳 -->
        <div class="sample-btn-container" id="sample-btn-container">
            <button class="add-sample-btn" id="add-sample-btn-camera">
	    <i class="bi bi-camera-video" style="transform: scale(1.5);"></i>攝像頭
	    </button>
            <button class="add-sample-btn" id="add-sample-btn-upload">
	    <i class="bi bi-cloud-upload" style="transform: scale(1.5);"></i>上傳
	    </button>
        </div>

        <!-- 攝像頭功能頁 -->
        <div class="add-sample-container" id="add-sample-camera">
            <button class="close-button" id="close-btn-camera"><i class="bi bi-x-circle-fill" style="transform: scale(1.5); color: red;"></i></button>
            <div class="text">請將臉部對齊圓圈。</div>	    
            <div class="box"> <video id="video" autoplay="true" playsinline></video> 
	    <div class="facefilter"></div></div>              
            <button class="norm-button" id="analyzeBtn-camera">開始分析</button>
        </div>

	<!-- 上傳圖像功能頁 -->
        <div class="add-sample-container" id="add-sample-upload">
            <button class="close-button" id="close-btn-upload"><i class="bi bi-x-circle-fill" style="transform: scale(1.5); color: red;"></i></button>
	    <!-- 隱藏的 input file 按鈕 -->
    	    <input type="file" id="hiddenFileInput" style="display: none;" accept="image/png, image/jpeg">
	    <button class="norm-button" id="upload-btn">從裝置上傳</button>
            <div class="text">預覽圖像</div>
            <div class="box" id="UploadSamplePreview"></div>
            <button class="norm-button" id="analyzeBtn-upload">開始分析</button>
        </div>
    </div>

    <!-- 伺服器回傳的結果頁面-處理後肌肉視覺化圖像 & 表情 & 使用到肌肉 -->
    <div class="norm-container" id="containerd">
        <div class="result-container" id="result-exp-mu">
            <div class="text">伺服器回傳的結果</div>
            <div class="box" id="result-img"></div>
	    <div class="text">情緒</div>
	    <div id="emotion-container"></div>
	    <div class="text">使用到的肌肉部位</div>
	    <div id="mu-container"></div>
        </div>
    </div>

    <!-- 伺服器回傳的結果頁面-處理後臉部比例圖像 & 臉部比例 -->
    <div class="norm-container" id="containerF">
        <!-- 容器 伺服器回傳的結果 -->
        <div class="result-container" id="result-face-area">
            <div class="text">伺服器回傳的結果</div>
            <div class="box" id="result-face"></div>
            <div class="text">左右面部對稱比例:</div>	    
        </div>
    </div>
</div>
    <script>
    document.addEventListener('DOMContentLoaded', function() {
      const sidebarBtn = document.getElementById('sidebarBtn');
      const settingBtn = document.getElementById('settingBtn');
      const sidebar = document.getElementById('sidebar');
      const homeBtn = document.getElementById('homeBtn');
      const emotionBtn = document.getElementById('emotionBtn');
      
      // 當點擊sidebar按钮
      sidebarBtn.addEventListener('click', function(event) {
        sidebar.style.left = '0'; // 顯示左側選單
        event.stopPropagation(); // 阻止事件冒泡
      });

      // 當點擊setting按钮
      settingBtn.addEventListener('click', function(event) {
        event.stopPropagation(); // 阻止事件冒泡
      });

      // 當點擊空白處
      document.addEventListener('click', function(event) {
        const target = event.target;
        if (!sidebar.contains(target) && target !== sidebarBtn) {
          sidebar.style.left = '-400px'; // 隱藏左側選單
        }
      });

      // 點擊"主頁"
      homeBtn.addEventListener('click', function() {
        window.location.href = 'https://1e1c-210-59-96-137.ngrok-free.app/web1'; 
      });

      // 點擊"表情辨識 & 顯示動作肌群"
      emotionBtn.addEventListener('click', function() {
        window.location.href = 'https://1e1c-210-59-96-137.ngrok-free.app/web2'; 
      });
    });

// 處理情緒結果函數 
function createEmotionMessage(emotion) {
    const expmessage = document.createElement('div');
    expmessage.classList.add('expmessage');

    const expicon = document.createElement('div');
    expicon.classList.add('expicon');

    // 根据情绪類别設置圖標樣式
    let expiconHTML = ''; // 初始化圖標樣式
    
    switch (emotion) {
        case 'happy':
            expiconHTML = '<i class="bi bi-emoji-smile-fill" style="color: #c2b700;"></i>'; // 情緒快樂使用該圖標
            break;
        case 'angry':
            expiconHTML = '<i class="bi bi-emoji-angry-fill" style="color: red;"></i>'; // 情緒生氣使用該圖標
            break;
        default:
            expiconHTML = '<i class="bi bi-emoji-neutral-fill" style="color: gray;"></i>'; // 情緒無表情使用該圖標
            break;
    }
    
    // 将圖標添加到icon元素中
    expicon.innerHTML = expiconHTML;

    const exptext = document.createElement('div');
    exptext.textContent = emotion; // 設定情绪類別文字

    expmessage.appendChild(expicon);
    expmessage.appendChild(exptext);

    return expmessage;
}

// 顯示情緒結果函數
function addEmotionMessages(emotion) {
    const emotionContainer = document.getElementById('emotion-container');
    emotionContainer.innerHTML = ''; // 清空容器中的旧消息

    const emotionMessage = createEmotionMessage(emotion); // 创建带有情绪的消息
    emotionContainer.appendChild(emotionMessage); // 将情绪消息添加到情绪容器中

    // 添加滑入效果
    setTimeout(() => {
        emotionMessage.classList.add('visible');
    }, 200);
}

// 處理每個肌肉的名稱和顏色函數
function createmuMessage(muscle) {
    const mumessage = document.createElement('div');
    mumessage.classList.add('mumessage');

    const muicon = document.createElement('div');
    muicon.classList.add('muicon');
    muicon.style.backgroundColor = muscle.color;  // 使用返回的肌肉色碼

    const mutext = document.createElement('div');
    mutext.textContent = muscle.name;  // 使用返回的肌肉名稱

    mumessage.appendChild(muicon);
    mumessage.appendChild(mutext);

    return mumessage;
}

// 顯示肌肉使用部位與對應顏色函數
function addmuMessages(muscles) {
    const mucontainer = document.getElementById('mu-container');
    mucontainer.innerHTML = '';  // 清空容器中的舊消息

    muscles.forEach((muscle, index) => {
        const mumessage = createmuMessage(muscle);  // 創建帶有名稱和顏色的消息
        mucontainer.appendChild(mumessage);

        // 使用 setTimeout 增加滑入效果
        setTimeout(() => {
            mumessage.classList.add('visible');
        }, index * 200);
    });
}

        const AddSampleBtnCamera = document.getElementById("add-sample-btn-camera");
	const AddSampleBtnUpload = document.getElementById("add-sample-btn-upload");
        const CloseBtnCamera = document.getElementById("close-btn-camera");
	const CloseBtnUpload = document.getElementById("close-btn-upload");
        const SampleBtnContainer = document.getElementById("sample-btn-container");
        const AddSampleCamera = document.getElementById("add-sample-camera");
	const AddSampleUpload = document.getElementById("add-sample-upload");
        const ReservedMenuBtn = document.getElementById("reserved-menu-btn");
        const dropdownMenu = document.getElementById("reserved-menu");
        const ModelSettingsBtn = document.getElementById("model-settings-btn");
        const ModelSettingsMenu = document.getElementById("model-settings-menu");
        const ModelSettingsTipIcon = document.getElementById('model-settings-tip-icon');
        const ModelSettingsTip = document.getElementById('model-settings-tip');
	const UploadBtn = document.getElementById('upload-btn');
	const fileInput = document.getElementById('fileInput');

// 獲取攝像頭影像並顯示在 video 元素中
navigator.mediaDevices.getUserMedia({ video: true })
    .then(stream => {
        const video = document.getElementById('video'); // 確保 video 元素存在
        video.srcObject = stream;
        video.play();
     })
    .catch(err => {
        console.error('Error accessing the camera:', err);
    });

// 監聽攝像頭頁面 "分析" 按鈕的點擊事件
document.getElementById('analyzeBtn-camera').addEventListener('click', captureAndSendImage);

// 將攝像頭影像發送到伺服器
function captureAndSendImage() {
    const video = document.getElementById('video');
    const canvas = document.createElement('canvas');
    const size = Math.min(video.videoWidth, video.videoHeight); // 截取video的尺寸
    canvas.width = size;
    canvas.height = size; 
    const ctx = canvas.getContext('2d');
    const x = (video.videoWidth - size) / 2; // 計算截取的起點坐標
    const y = (video.videoHeight - size) / 2;

    ctx.drawImage(video, x, y, size, size, 0, 0, size, size); // 繪製截取的部分到video

    // 獲取選擇的模型標籤
    const selectedModelLabel = document.getElementById('modelSelect').value;

    // 將圖像轉換為 Blob 物件
    canvas.toBlob(blob => {
        // 創建表單數據物件
        const formData = new FormData();
        formData.append('file', blob, 'captured_image.jpg'); // 圖像文件名，修改為 JPEG 格式
        formData.append('model_label', selectedModelLabel);  // 發送模型標籤

        // 發送 POST 請求
        fetch("https://1e1c-210-59-96-137.ngrok-free.app/emotion_recognition", {
            method: 'POST',
            body: formData
        })
        .then(response => response.json())
        .then(data => {
            // 處理返回的圖像數據
            if (data.image) {
                const ResultImg = document.getElementById('result-img');
                const imgBase64 = data.image;
                const imgElement = document.createElement('img');
                imgElement.src = 'data:image/jpeg;base64,' + imgBase64.replace(/"/g, '');
                ResultImg.innerHTML = '';  // 清空 ResultImg 中的內容
                ResultImg.appendChild(imgElement);  // 將圖像元素添加到 ResultImg 中
            }
              // 處理返回的辨識結果
              const predictionResult = data.result;
              const emotion = predictionResult; // 獲取情緒類別的地方
              addEmotionMessages(emotion); // 調用 addEmotionMessages 函數

              // 處理返回的肌肉名稱和顏色
              const muNames = data.muresult ? data.muresult.split(', ') : []; // 檢查 muresult 是否存在
              const muColors = data.mu_colors ? data.mu_colors.split(', ') : []; // 檢查 mu_colors 是否存在

              // 將名稱和顏色組合為對象，確保長度匹配
              const muscles = muNames.map((name, index) => ({
              name: name,
              color: muColors[index] || "#000000" // 如果顏色未定義，使用預設顏色
              }));

              // 傳遞肌肉數據到 addmuMessages 函數
              addmuMessages(muscles);
        }) 
        .catch(error => console.error('Error receiving image:', error));

    }, 'image/jpeg'); // 修改圖像格式為 JPEG
}


// 監聽上傳影像頁面 "分析" 按鈕的點擊事件
document.getElementById('analyzeBtn-upload').addEventListener('click', () => {
    const UploadSamplePreviewBox = document.getElementById('UploadSamplePreview');
    const hiddenFileInput = document.getElementById('hiddenFileInput');
    const uploadedImage = hiddenFileInput.files[0];
    UploadSamplePreviewBox.innerHTML = '';
    // 獲取選擇的模型標籤
    const selectedModelLabel = document.getElementById('modelSelect').value;

    if (uploadedImage) {
        // 上傳圖像的情境
        const reader = new FileReader();
        reader.onload = function (event) {
            const img = new Image();
            img.src = event.target.result;
            img.onload = function () {
                const canvas = document.createElement('canvas');
                const size = Math.min(img.width, img.height); // 截取預覽圖像區域
                canvas.width = size;
                canvas.height = size; // 正方形
                const ctx = canvas.getContext('2d');
                const x = (img.width - size) / 2; // 計算截取的起點坐標
                const y = (img.height - size) / 2;

                // 翻轉上傳圖像
                ctx.save(); // 保存當前狀態
                ctx.translate(size, 0); // 移動到 Canvas 的右側
                ctx.scale(-1, 1); // 水平翻轉
                ctx.drawImage(img, x, y, size, size, 0, 0, size, size); // 繪製截取的部分到預覽圖像區域上
                ctx.restore(); // 恢復之前的狀態

                // 將圖像轉換為 Blob 物件
                canvas.toBlob(blob => {
                    // 創建表單數據物件
                    const formData = new FormData();
                    formData.append('file', blob, 'uploaded_image.jpg'); // 圖像文件名，修改為 JPEG 格式
                    formData.append('model_label', selectedModelLabel);  // 發送模型標籤

                    // 發送 POST 請求
                    fetch("https://1e1c-210-59-96-137.ngrok-free.app/emotion_recognition", {
                        method: 'POST',
                        body: formData
                    })
                    .then(response => response.json())
                    .then(data => {
                        // 清空 result-img 以防有舊的圖像
                        const ResultImg = document.getElementById('result-img');
                        ResultImg.innerHTML = '';

                        // 處理返回的圖像數據
                        if (data.image) {
                            const imgBase64 = data.image;
                            const imgElement = document.createElement('img');
                            imgElement.src = 'data:image/jpeg;base64,' + imgBase64.replace(/"/g, '');
                            ResultImg.appendChild(imgElement);  // 將圖像元素添加到 ResultImg 中
                        }

                           // 處理返回的辨識結果
                           const predictionResult = data.result;
                           const emotion = predictionResult; // 獲取情緒類別的地方
                           addEmotionMessages(emotion); // 調用 addEmotionMessages 函數

                           // 處理返回的肌肉名稱和顏色
                           const muNames = data.muresult ? data.muresult.split(', ') : []; // 檢查 muresult 是否存在
                           const muColors = data.mu_colors ? data.mu_colors.split(', ') : []; // 檢查 mu_colors 是否存在

                           // 將名稱和顏色組合為對象，確保長度匹配
                           const muscles = muNames.map((name, index) => ({
                           name: name,
                           color: muColors[index] || "#000000" // 如果顏色未定義，使用預設顏色
                           }));

                           // 傳遞肌肉數據到 addmuMessages 函數
                           addmuMessages(muscles);
                    })
                    .catch(error => console.error('Error receiving image:', error));

                }, 'image/jpeg'); // 修改圖像格式為 JPEG
            };
        };
        reader.readAsDataURL(uploadedImage); // 讀取上傳的圖像

        // 清空上傳的圖像檔案，這樣下次選擇相同的圖像時不會再被讀取
        hiddenFileInput.value = '';
    } else {
        // 若無上傳的圖像，則顯示提示訊息
        alert('請先上傳圖像！');
      }
});
        // 當按下攝像頭添加樣本，顯示功能頁面
        AddSampleBtnCamera.addEventListener("click", () => {
            ModelSettingsBtn.style.display = "block";
            SampleBtnContainer.style.display = "none";  // 隱藏添加樣本頁面
	    AddSampleUpload.style.display = "none";  // 隱藏上傳添加樣本頁面		
            AddSampleCamera.style.display = "flex";  // 顯示攝像頭添加頁面
            dropdownMenu.classList.remove("show"); // 隱藏預留按鈕
        });

	// 當按下上傳添加樣本，顯示功能頁面
        AddSampleBtnUpload.addEventListener("click", () => {
            ModelSettingsBtn.style.display = "block";
            SampleBtnContainer.style.display = "none";  // 隱藏添加樣本頁面
	    AddSampleCamera.style.display = "none";  // 隱藏攝像頭添加頁面		
            AddSampleUpload.style.display = "flex";  // 顯示上傳添加樣本頁面
            dropdownMenu.classList.remove("show"); // 隱藏預留按鈕
        });


        // 當按下關閉按鈕時，隱藏模型設定按鈕 & 上傳添加樣本 & 攝像頭添加頁面
        const handleCloseButtonClick = () => {
    	ModelSettingsBtn.style.display = "none";  // 隱藏模型設定按鈕
    	AddSampleCamera.style.display = "none";  // 隱藏攝像頭添加頁面
    	AddSampleUpload.style.display = "none";  // 隱藏上傳添加樣本頁面
    	SampleBtnContainer.style.display = "flex";  // 顯示添加樣本頁面
    	dropdownMenu.classList.remove("show"); // 隱藏預留按鈕
	};

	CloseBtnCamera.addEventListener("click", handleCloseButtonClick);
	CloseBtnUpload.addEventListener("click", handleCloseButtonClick);

        // 當按下預留按鈕時，顯示選單
        ReservedMenuBtn.addEventListener("click", (event) => {
            event.stopPropagation();  // 阻止點擊事件冒泡到body
            dropdownMenu.classList.toggle("show");// 顯示預留按鈕
            ModelSettingsMenu.classList.remove("show"); // 隱藏設定選單
        });

        // 當按下模型設定按鈕時，顯示選單
        ModelSettingsBtn.addEventListener("click", (event) => {
            event.stopPropagation();  // 阻止點擊事件冒泡到body
            ModelSettingsMenu.classList.toggle("show");
            dropdownMenu.classList.remove("show"); // 隱藏預留按鈕
        });

        // 當點擊頁面空白處時，隱藏預留按鈕和模型設定選單
        document.addEventListener("click", (event) => {
            if (!dropdownMenu.contains(event.target) && !ReservedMenuBtn.contains(event.target)) {
                dropdownMenu.classList.remove("show");
            }
            if (!ModelSettingsMenu.contains(event.target) && !ModelSettingsBtn.contains(event.target)) {
                ModelSettingsMenu.classList.remove("show");
            }
        });

        // 監聽鼠標進入事件，顯示提示訊息並追蹤游標位置
        ModelSettingsTipIcon.addEventListener('mouseenter', function(event) {
            ModelSettingsTip.style.display = 'block'; // 顯示提示訊息
        });

        // 監聽鼠標離開事件，隱藏提示訊息
        ModelSettingsTipIcon.addEventListener('mouseleave', function() {
            ModelSettingsTip.style.display = 'none'; // 隱藏提示訊息
        });

	// 點擊自訂按鈕時觸發隱藏的 input file 按鈕
        UploadBtn.addEventListener('click', function() {
            hiddenFileInput.click();
        });

        // 當檔案被選擇後觸發這個事件
        hiddenFileInput.addEventListener('change', function(e) {
    	const file = hiddenFileInput.files[0];
    	const reader = new FileReader();

    	// 確保檔案存在，並且是圖片檔案
    	if (file && (file.type === 'image/png' || file.type === 'image/jpeg')) {
        	reader.readAsDataURL(file); // 讀取檔案為 Data URL

        	// 當檔案讀取完成後，將其內容顯示為圖片
        	reader.onload = function(e) {
            	const img = new Image();
            	img.src = e.target.result;
            	img.style.maxWidth = '100%'; // 控制圖片大小以適應頁面
            	img.style.height = '100%'; // 使圖片高度填滿容器
            	img.style.objectFit = 'contain'; // 確保圖片不會變形
            	UploadSamplePreview.innerHTML = ''; // 清空之前的內容
            	UploadSamplePreview.appendChild(img); // 顯示圖片           	
           }
    	} else {
        	fileDisplayArea.innerText = '請上傳 PNG 或 JPG 格式的圖片。';
		    	}
	});
        
    </script>
</body>

</html>


"""

