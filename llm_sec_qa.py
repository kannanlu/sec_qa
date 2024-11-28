#%% Installs Unsloth, Xformers (Flash Attention) and all other packages
!pip install "unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git"
!pip install --no-deps xformers trl peft accelerate bitsandbytes
!pip install sec_api
!pip install -U langchain
!pip install -U langchain-community
!pip install -U sentence-transformers
!pip install -U faiss-gpu

#%%
# huggingface token for accessing gated models like LLaMa 3 8B Instruct
hf_token = ""
# SEC-API Key, free api key allow 100 calls per day
sec_api_key = ""

#%%