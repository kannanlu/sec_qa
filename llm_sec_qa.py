    """ An AI RAG tool fine-tuned for SEC 10-K filing analysis, based on open source 8B LLM model. 
    To use it, one just feed in the 10-K file of a company that you are interested in and then 
    this tool will vectorize the paragraph you input for Retrieval module, and Augument with 
    the prompt, and then Generate possible answer accordingly (RAG). This is an adaptation of
    work by Adam Lucek at
    https://colab.research.google.com/drive/1eisDW1zTuHgHzoPS8o8AKfPThQ3rWQ2P
    """
#%%
# NOTE do not put any api key to online repo !!!
# huggingface token for accessing gated models like LLaMa 3 8B Instruct
hf_token = ""
# SEC-API Key, free api key allow 100 calls per day
sec_api_key = ""

#%%
# Fine Tuning Related Packages
from unsloth import FastLanguageModel
import torch
from datasets import load_dataset
from trl import SFTTrainer
from transformers import TrainingArguments
from unsloth import is_bfloat16_supported

# Pipeline & RAG Related Packages
from sec_api import ExtractorApi, QueryApi
from langchain.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_text_splitters import RecursiveCharacterTextSplitter




#%% **Part 1: Fine Tuning LLaMa 3 with Unsloth**
""" ** load pre-trained model
We will be using the built in GPU on local/Linux to do all our fine tuning needs, using the [Unsloth Library](https://github.com/unslothai/unsloth).

Much of the below code is augmented from [Unsloth Documentation!](https://colab.research.google.com/drive/135ced7oHytdxu3N2DNe1Z0kqjyYIkDXp?usp=sharing#scrollTo=AEEcJ4qfC7Lp)

### **Initializing Pre Trained Model and Tokenizer**

For this example we will be using Meta's [LLaMa 3 8b Instruct Model](https://huggingface.co/meta-llama/Meta-Llama-3-8B-Instruct).  
 **NOTE**: This is a gated model, you must request access on HF and pass in your HF token in the below step.
"""

# Load the model and tokenizer from the pre-trained FastLanguageModel
model, tokenizer = FastLanguageModel.from_pretrained(
    # Specify the pre-trained model to use
    model_name = "meta-llama/Meta-Llama-3-8B-Instruct",
    # Specifies the maximum number of tokens (words or subwords) that the model can process in a single forward pass
    max_seq_length = 2048,
    # Data type for the model. None means auto-detection based on hardware, Float16 for specific hardware like Tesla T4
    dtype = None,
    # Enable 4-bit quantization, By quantizing the weights of the model to 4 bits instead of the usual 16 or 32 bits, the memory required to store these weights is significantly reduced. This allows larger models to be run on hardware with limited memory resources.
    load_in_4bit = True,
    # Access token for gated models, required for authentication to use models like Meta-Llama-2-7b-hf
    token = hf_token,
)

#%%
"""**Adding in LoRA adapters for parameter efficient fine tuning**

LoRA, or Low-Rank Adaptation, is a technique used in machine learning to fine-tune large models more efficiently. It works by adding a small, additional set of parameters to the existing model instead of retraining all the parameters from scratch. This makes the fine-tuning process faster and less resource-intensive. Essentially, LoRA helps tailor a pre-trained model to specific tasks or datasets without requiring extensive computational power or memory.
"""

# Apply LoRA (Low-Rank Adaptation) adapters to the model for parameter-efficient fine-tuning
model = FastLanguageModel.get_peft_model(
    model,
    # Rank of the adaptation matrix. Higher values can capture more complex patterns. Suggested values: 8, 16, 32, 64, 128
    r = 16,
    # Specify the model layers to which LoRA adapters should be applied
    target_modules = ["q_proj", "k_proj", "v_proj", "o_proj",
                      "gate_proj", "up_proj", "down_proj"],
    # Scaling factor for LoRA. Controls the weight of the adaptation. Typically a small positive integer
    lora_alpha = 16,
    # Dropout rate for LoRA. A value of 0 means no dropout, which is optimized for performance
    lora_dropout = 0,
    # Bias handling in LoRA. Setting to "none" is optimized for performance, but other options can be used
    bias = "none",
    # Enables gradient checkpointing to save memory during training. "unsloth" is optimized for very long contexts
    use_gradient_checkpointing = "unsloth",
    # Seed for random number generation to ensure reproducibility of results
    random_state = 3407,
)

"""1. **r**: The rank of the low-rank adaptation matrix. This determines the capacity of the adapter to capture additional information. Higher ranks allow capturing more complex patterns but also increase computational overhead.

2. **target_modules**: List of module names within the model to which the LoRA adapters should be applied. These modules typically include the projections within transformer layers (e.g., query, key, value projections) and other key transformation points.
  - **q_proj**: Projects input features to query vectors for attention mechanisms.
  - **k_proj**: Projects input features to key vectors for attention mechanisms.
  - **v_proj**: Projects input features to value vectors for attention mechanisms.
  - **o_proj**: Projects the output of the attention mechanism to the next layer.
  - **gate_proj**: Applies gating mechanisms to modulate the flow of information.
  - **up_proj**: Projects features to a higher dimensional space, often used in feed-forward networks.
  - **down_proj**: Projects features to a lower dimensional space, often used in feed-forward networks.

These layers are typically involved in transformer-based models, facilitating various projections and transformations necessary for the attention mechanism and feed-forward processes.

3. **lora_alpha**: A scaling factor for the LoRA adapter. It controls the impact of the adapter on the model's outputs. Typically set to a small positive integer.

4. **lora_dropout**: Dropout rate applied to the LoRA adapters. Dropout helps in regularizing the model, but setting it to 0 means no dropout, which is often optimal for performance.

5. **bias**: This specifies how biases should be handled in the LoRA adapters. Setting it to "none" indicates no bias is used, which is optimized for performance, although other options are available depending on the use case.

6. **use_gradient_checkpointing**: Enables gradient checkpointing, which helps to save memory during training by not storing all intermediate activations. "unsloth" is a setting optimized for very long contexts, but it can also be set to True.

7. **random_state**: A seed for the random number generator to ensure reproducibility. This makes sure that the results are consistent across different runs of the code.

### **Preparing the Fine Tuning Dataset**

We will be using a HF dataset of Financial Q&A over form 10ks, provided by user [Virat Singh](https://github.com/virattt) here https://huggingface.co/datasets/virattt/llama-3-8b-financialQA

The following code below formats the entries into the prompt defined first for training, being careful to add in special tokens. In this case our End of Sentence token is <|eot_id|>. More LLaMa 3 special tokens [here](https://llama.meta.com/docs/model-cards-and-prompt-formats/meta-llama-3/)
"""

#%%
# Defining the expected prompt
ft_prompt = """<|begin_of_text|><|start_header_id|>system<|end_header_id|>
Below is a user question, paired with retrieved context. Write a response that appropriately answers the question,
include specific details in your response. <|eot_id|>

<|start_header_id|>user<|end_header_id|>

### Question:
{}

### Context:
{}

<|eot_id|>

### Response: <|start_header_id|>assistant<|end_header_id|>
{}"""

# Grabbing end of sentence special token
EOS_TOKEN = tokenizer.eos_token # Must add EOS_TOKEN

# Function for formatting above prompt with information from Financial QA dataset
def formatting_prompts_func(examples):
    questions = examples["question"]
    contexts       = examples["context"]
    responses      = examples["answer"]
    texts = []
    for question, context, response in zip(questions, contexts, responses):
        # Must add EOS_TOKEN, otherwise your generation will go on forever!
        text = ft_prompt.format(question, context, response) + EOS_TOKEN
        texts.append(text)
    return { "text" : texts, }
pass

dataset = load_dataset("virattt/llama-3-8b-financialQA", split = "train")
dataset = dataset.map(formatting_prompts_func, batched = True,)
#%% check dataset
dataset[0]

#%%
"""### **Defining the Trainer Arguments**

We will be setting up and using HuggingFace Transformer Reinforcement Learning (TRL)'s [Supervised Fine-Tuning Trainer](https://huggingface.co/docs/trl/sft_trainer)

**Supervised fine-tuning** is a process in machine learning where a pre-trained model is further trained on a specific dataset with labeled examples. 
During this process, the model learns to make predictions or classifications based on the labeled data, 
improving its performance on the specific task at hand. 
This technique leverages the general knowledge the model has already acquired during its initial training phase 
and adapts it to perform well on a more targeted set of examples. 
Supervised fine-tuning is commonly used to customize models for specific applications, such as sentiment analysis, object recognition, 
or language translation, by using task-specific annotated data.
"""

trainer = SFTTrainer(
    # The model to be fine-tuned
    model = model,
    # The tokenizer associated with the model
    tokenizer = tokenizer,
    # The dataset used for training
    train_dataset = dataset,
    # The field in the dataset containing the text data
    dataset_text_field = "text",
    # Maximum sequence length for the training data
    max_seq_length = 2048,
    # Number of processes to use for data loading
    dataset_num_proc = 2,
    # Whether to use sequence packing, which can speed up training for short sequences
    packing = False,
    args = TrainingArguments(
        # Batch size per device during training
        per_device_train_batch_size = 2,
        # Number of gradient accumulation steps to perform before updating the model parameters
        gradient_accumulation_steps = 4,
        # Number of warmup steps for learning rate scheduler
        warmup_steps = 5,
        # Total number of training steps
        max_steps = 60,
        # Number of training epochs, can use this instead of max_steps, for this notebook its ~900 steps given the dataset
        # num_train_epochs = 1,
        # Learning rate for the optimizer
        learning_rate = 2e-4,
        # Use 16-bit floating point precision for training if bfloat16 is not supported
        fp16 = not is_bfloat16_supported(),
        # Use bfloat16 precision for training if supported
        bf16 = is_bfloat16_supported(),
        # Number of steps between logging events
        logging_steps = 1,
        # Optimizer to use (in this case, AdamW with 8-bit precision)
        optim = "adamw_8bit",
        # Weight decay to apply to the model parameters
        weight_decay = 0.01,
        # Type of learning rate scheduler to use
        lr_scheduler_type = "linear",
        # Seed for random number generation to ensure reproducibility
        seed = 3407,
        # Directory to save the output models and logs
        output_dir = "outputs",
    ),
)

"""1. **model**: The model to be fine-tuned. This is the pre-trained model that will be adapted to the specific training data.

2. **tokenizer**: The tokenizer associated with the model. It converts text data into tokens that the model can process.

3. **train_dataset**: The dataset used for training. This is the collection of labeled examples that the model will learn from during the fine-tuning process.

4. **dataset_text_field**: The field in the dataset containing the text data. This specifies which part of the dataset contains the text that the model will be trained on.

5. **max_seq_length**: The maximum sequence length for the training data. This limits the number of tokens per input sequence to ensure they fit within the model's processing capacity.

6. **dataset_num_proc**: The number of processes to use for data loading. This can speed up data loading by parallelizing it across multiple processes.

7. **packing**: A boolean indicating whether to use sequence packing. Sequence packing can speed up training by combining multiple short sequences into a single batch.

8. **args**: A set of training arguments that configure the training process. These include various hyperparameters and settings:

    - **per_device_train_batch_size**: The batch size per device during training. This determines how many examples are processed together in one forward/backward pass.
    
    - **gradient_accumulation_steps**: The number of gradient accumulation steps to perform before updating the model parameters. This allows for effectively larger batch sizes without requiring more memory.
    
    - **warmup_steps**: The number of warmup steps for the learning rate scheduler. During these steps, the learning rate increases gradually to the initial value.
    
    - **max_steps**: The total number of training steps. This defines how many batches of training data the model will be trained on.
    
    - **num_train_epochs**: The number of training epochs (uncommented in the example). This defines how many times the entire training dataset will be passed through the model.
    
    - **learning_rate**: The learning rate for the optimizer. This controls how much to adjust the model's weights with respect to the gradient during training.
    
    - **fp16**: A boolean indicating whether to use 16-bit floating point precision for training if bfloat16 is not supported. This can speed up training and reduce memory usage.
    
    - **bf16**: A boolean indicating whether to use bfloat16 precision for training if supported. This can also speed up training and reduce memory usage while maintaining higher precision than fp16.
    
    - **logging_steps**: The number of steps between logging events. This controls how frequently training progress and metrics are logged.
    
    - **optim**: The optimizer to use. In this case, AdamW with 8-bit precision, which can improve efficiency for large models.
    
    - **weight_decay**: The weight decay to apply to the model parameters. This is a regularization technique to prevent overfitting by penalizing large weights.
    
    - **lr_scheduler_type**: The type of learning rate scheduler to use. This controls how the learning rate changes over time during training.
    
    - **seed**: The seed for random number generation. This ensures reproducibility of results by controlling the randomness in training.
    
    - **output_dir**: The directory to save the output models and logs. This specifies where the trained model and training logs will be stored.

# **Now We're Ready to Train!** 
"""
#%% train

trainer_stats = trainer.train()

#%%
"""---
### **Saving Your Fine-Tuned Model Locally**

"""

model.save_pretrained("/checkpoint/l3_finagent_step60") # Local saving
tokenizer.save_pretrained("/checkpoint/l3_finagent_step60")

#%% model re-load
"""### **Function for Loading Your New Model Later**

So you don't have to retrain every time, just turn False to True and run this. One note, Colab deletes all files, so mount your google drive and save to a folder there to keep it persistent!

You'll still need to download and run the python packages from the top of the notebook, and also re define the prompt for the inference functions down below
"""

# Redefining prompt if importing without training
ft_prompt = """<|begin_of_text|><|start_header_id|>system<|end_header_id|>
Below is a user question, paired with retrieved context. Write a response that appropriately answers the question,
include specific details in your response. <|eot_id|>

<|start_header_id|>user<|end_header_id|>

### Question:
{}

### Context:
{}

<|eot_id|>

### Response: <|start_header_id|>assistant<|end_header_id|>
{}"""

if False: # switch to true to load model back up
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name = "/checkpoint/l3_finagent_step60", # Path into where you saved your model
        max_seq_length = 2048, # Existing arguments from when we loaded earlier
        dtype = None,
        load_in_4bit = True,
    )
    FastLanguageModel.for_inference(model)
    
#%%
"""### **Setting Up Functions for Running Inference**

**Inference** refers to the process of using a trained machine learning model to make predictions or generate outputs based on new, unseen input data. 
It involves feeding the input data into the model and obtaining the model's predictions, classifications, or generated text, 
depending on the task the model is designed for. Inference is the application phase of the model, as opposed to the training phase.
"""

# Main Inference Function, handles generating and decoding tokens
def inference(question, context):
  inputs = tokenizer(
  [
      ft_prompt.format(
          question,
          context,
          "", # output - leave this blank for generation!
      )
  ], return_tensors = "pt").to("cuda")

  # Generate tokens for the input prompt using the model, with a maximum of 64 new tokens.
  # The `use_cache` parameter enables faster generation by reusing previously computed values.
  # The `pad_token_id` is set to the EOS token to handle padding properly.
  outputs = model.generate(**inputs, max_new_tokens = 64, use_cache = True, pad_token_id=tokenizer.eos_token_id)
  response = tokenizer.batch_decode(outputs) # Decoding tokens into english words
  return response

# Function for extracting just the language model generation from the full response
def extract_response(text):
    text = text[0]
    start_token = "### Response: <|start_header_id|>assistant<|end_header_id|>"
    end_token = "<|eot_id|>"

    start_index = text.find(start_token) + len(start_token)
    end_index = text.find(end_token, start_index)

    if start_index == -1 or end_index == -1:
        return None

    return text[start_index:end_index].strip()

#%% local test of inference 
context = "The increase in research and development expense for fiscal year 2023 was primarily driven by increased compensation, employee growth, engineering development costs, and data center infrastructure."
question = "What were the primary drivers of the notable increase in research and development expenses for fiscal year 2023?"

resp = inference(question, context)
parsed_response = extract_response(resp)
print(parsed_response)

print(resp)

#%% **Part 2: Setting Up SEC 10-K Data Pipeline & Retrieval Functionality**
"""---

Now that we have our fine tuned language model, inference functions, and a desired prompt format, we need to now set up the RAG pipeline to inject the relevant context into each generation.

The flow will follow as such:

*User Question* -> *Context Retrieval from 10-K* -> *LLM Answers User Question Using Context*

To do this we will need to be able to:
1. Gather specific from 10-K's
2. Parse and chunk the text in them
3. Vectorize and embed the chunks into a vector Database
4. Set up a retriever to semantically search the user's questions over the database to return relevant context

A **Form 10-K** is an annual report required by the U.S. Securities and Exchange Commission, that gives a comprehensive summary of a company's financial performance.

### **Function For 10-K Retrieval**

To do this easier, we're taking advantage of the SEC API https://sec-api.io/. It is free to sign up, and you get 100 API calls a day to use, each time we load a ticker's symbol it will use 3 calls.

For this project, we'll be focused on loading only sections **1A** and **7**
- **1A**: Risk Factors
- **7**: Management's Discussion and Analysis of Financial Condition and Results of Operations
"""

# Extract Filings Function
def get_filings(ticker):
    global sec_api_key

    # Finding Recent Filings with QueryAPI
    queryApi = QueryApi(api_key=sec_api_key)
    query = {
      "query": f"ticker:{ticker} AND formType:\"10-K\"",
      "from": "0",
      "size": "1",
      "sort": [{ "filedAt": { "order": "desc" } }]
    }
    filings = queryApi.get_filings(query)

    # Getting 10-K URL
    filing_url = filings["filings"][0]["linkToFilingDetails"]

    # Extracting Text with ExtractorAPI
    extractorApi = ExtractorApi(api_key=sec_api_key)
    onea_text = extractorApi.get_section(filing_url, "1A", "text") # Section 1A - Risk Factors
    seven_text = extractorApi.get_section(filing_url, "7", "text") # Section 7 - Management’s Discussion and Analysis of Financial Condition and Results of Operations

    # Joining Texts
    combined_text = onea_text + "\n\n" + seven_text

    return combined_text

#%%
"""### **Setting Up Embeddings Locally**

In the spirit of local and fine tuned models, we'll be using an open source embedding model, [Beijing Academy of Artificial Intelligence's - Large English Embedding Model](https://huggingface.co/BAAI/bge-large-en-v1.5). More details on their open source model available in their [GitHub repo](https://github.com/FlagOpen/FlagEmbedding)!

**Embeddings** are numerical representations of data, typically used to convert complex, high-dimensional data into a lower-dimensional space where similar data points are closer together. In the context of natural language processing (NLP), embeddings are used to represent words, phrases, or sentences as vectors of real numbers. These vectors capture semantic relationships, meaning that words with similar meanings are represented by vectors that are close together in the embedding space.

**Embedding models** are machine learning models that are trained to create these numerical representations. They learn to encode various types of data into embeddings that capture the essential characteristics and relationships within the data. For example, in NLP, embedding models like Word2Vec, GloVe, and BERT are trained on large text corpora to produce word embeddings. These embeddings can then be used for various downstream tasks, such as text classification, sentiment analysis, or machine translation. In this case we'll be using it for semantic similarity
"""

# HF Model Path
modelPath = "BAAI/bge-large-en-v1.5"
# Create a dictionary with model configuration options, specifying to use the cuda for GPU optimization
model_kwargs = {'device':'cuda'}
encode_kwargs = {'normalize_embeddings': True}

# Initialize an instance of LangChain's HuggingFaceEmbeddings with the specified parameters
embeddings = HuggingFaceEmbeddings(
    model_name=modelPath,     # Provide the pre-trained model's path
    model_kwargs=model_kwargs, # Pass the model configuration options
    encode_kwargs=encode_kwargs # Pass the encoding options
)

#%%
"""### **Processing & Defining the Vector Database**

In this flow we get the data from the above defined SEC API functions, and then go through Three steps:
1. Text Splitting
2. Vectorizing
3. Retrieval Function Setup

**Text splitting** is the process of breaking down large documents or text data into smaller, manageable chunks. This is often necessary when dealing with extensive text data, such as legal documents, financial reports, or any lengthy articles. The purpose of text splitting is to ensure that the data can be effectively processed, analyzed, and indexed by machine learning models and databases.

**Vector databases** store data in the form of vectors, which are numerical representations of text, images, or other types of data. These vectors capture the semantic meaning of the data, allowing for efficient similarity search and retrieval.

The Vector DB we're using here is the [Facebook AI Semantic Search](https://ai.meta.com/tools/faiss/) library, a lightweight an in memory (don't need to save this to a disk) solution that is not as powerful as other Vector DB's but will work great for this use case

**How They Use Split Documents and Embeddings Together:**
1. **Embeddings:** When text data is split into smaller chunks, each chunk is converted into a numerical vector (embedding) using an embedding model. These embeddings capture the semantic relationships and meaning of the text.
2. **Storage:** The vector database stores these embeddings along with references to the original text chunks.
3. **Indexing:** The database indexes the vectors to allow for fast and efficient similarity searches. This indexing process organizes the vectors in a way that makes it easy to find similar vectors quickly.
4. **Usage:** When a query is made, the vector database searches for the most similar vectors (text chunks) to the query vector, retrieving the relevant text chunks based on their semantic similarity.
"""

# Prompt the user to input the stock ticker they want to analyze
ticker = input("What Ticker Would you Like to Analyze? ex. AAPL: ")

print("-----")
print("Getting Filing Data")
# Retrieve the filing data for the specified ticker
filing_data = get_filings(ticker)

print("-----")
print("Initializing Vector Database")
# Initialize a text splitter to divide the filing data into chunks
text_splitter = RecursiveCharacterTextSplitter(
    chunk_size = 1000,         # Maximum size of each chunk
    chunk_overlap = 500,       # Number of characters to overlap between chunks
    length_function = len,     # Function to determine the length of the chunks
    is_separator_regex = False # Whether the separator is a regex pattern
)
# Split the filing data into smaller, manageable chunks
split_data = text_splitter.create_documents([filing_data])

# Create a FAISS vector database from the split data using embeddings
db = FAISS.from_documents(split_data, embeddings)

# Create a retriever object to search within the vector database
retriever = db.as_retriever()

print("-----")
print("Filing Initialized")

#%%
"""
### **Retrieval**

**Description:**
Retrieval is the process of querying a vector database to find and return relevant text chunks or documents that match a given query. This involves searching through the indexed embeddings to identify the ones that are most similar to the query.

**How It Works:**
1. **Query Embedding:** When a query is made, it is first converted into an embedding using the same embedding model used for the text chunks.
2. **Similarity Search:** The retriever searches the vector database for embeddings that are similar to the query embedding. This similarity is often measured using distance metrics like cosine similarity or Euclidean distance.
3. **Document Retrieval:** The retriever then retrieves the original text chunks or documents associated with the similar embeddings.
4. **Context Assembly:** The retrieved text chunks are assembled to provide a coherent context or answer to the query.

In this function, the query is used to invoke the retriever, which returns a list of documents. The content of these documents is then extracted and returned as the context for the query."""

# Retrieval Function
def retrieve_context(query):
    global retriever
    retrieved_docs = retriever.invoke(query) # Invoke the retriever with the query to get relevant documents
    context = []
    for doc in retrieved_docs:
        context.append(doc.page_content) # Collect the content of each retrieved document
    return context

context = retrieve_context("How have currency fluctuations impacted the company's net sales and gross margins?")
print(context)

#%%
"""---
# **Main Script: Putting it All Together!**

Now, we'll string everything together into a very simple while loop that will take the user's question, retrieve context from the Vector DB populated with the specific company Form 10-K, then run inference through our fine tuned model to generate a response! Give it a shot
"""

while True:
  question = input(f"What would you like to know about {ticker}'s form 10-K? ")
  if question == "x":
    break
  else:
    context = retrieve_context(question) # Context Retrieval
    resp = inference(question, context) # Running Inference
    parsed_response = extract_response(resp) # Parsing Response
    print(f"L3 Agent: {parsed_response}")
    print("-----\n")

"""What region contributes most to international sales?  
Where is outsourcing located currently?  
Does the US dollar weakening help or hurt the company?  
What are significant announcements of products during fiscal year 2023?  
iPhone Net Sales?

---
### Some thoughts
1. Really shows just how much processing is needed for training and running large, advanced models.
2. Even with a 16gb VRAM GPU still takes an hour to train one epoch, on just an 8 billion parameter small model with a narrow dataset
3. Even with all this time and effort put in, small models can be very powerful!
4. Latency is a big issue to solve in the lack of compute
5. A lot of innacuracy likely also due to 2048 token sequence length requirement

---
"""