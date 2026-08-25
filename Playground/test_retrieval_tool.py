import sys
from pathlib import Path

# Ensure project root is in path if running as a script
sys.path.append(str(Path(__file__).parent))

from tools.retrieval_tool import RetrievalTool

def run_retrieval_tests():
    print("=" * 60)
    print("Initializing RetrievalTool and loading artifacts...")
    print("=" * 60)
    
    # Initialize tool (loads chunks, embeddings, BM25 index, models, and LLM)
    tool = RetrievalTool()

    # Define test queries across different scenarios
    test_queries = [
        # 1. Answerable compliance / Basel III query
        "What are the minimum capital requirement rules under Basel III?",
        
        # 2. Query requiring specific regulatory detail
        "How is the credit conversion factor (CCF) applied to off-balance sheet exposures?",
        
        # 3. Off-topic query (Should fail to find answer grounded in Basel III context)
        "What is the average rainfall in California during winter?",
        
        # 4. Ambiguous query to test internal query rewriting/retry loop
        "Tell me about risk weights for residential mortgages."
    ]

    print("\nStarting Test Executions:\n")

    for idx, query in enumerate(test_queries, 1):
        print("=" * 60)
        print(f"Test {idx}: {query}")
        print("=" * 60)
        
        try:
            result = tool.run(query, max_retries=2, top_k_final=5)
            
            print(f"Success:           {result.success}")
            print(f"Retries Used:      {result.retries_used}")
            print(f"Final Query Used:  {result.query_used}")
            
            if result.success:
                print(f"\nAnswer:\n{result.answer}")
                print(f"\nCitations:        {result.citations}")
            else:
                print(f"\nReason (Feedback): {result.reason}")
                
            if result.results_df is not None and not result.results_df.empty:
                print("\nTop Retrieved Chunks:")
                for row in result.results_df[['chunk_id', 'rerank_score']].itertuples():
                    print(f"  - [{row.chunk_id}] Rerank Score: {row.rerank_score:.4f}")
                    
        except Exception as e:
            print(f"TEST FAILED WITH ERROR: {e}")
            
        print("\n")

if __name__ == "__main__":
    run_retrieval_tests()