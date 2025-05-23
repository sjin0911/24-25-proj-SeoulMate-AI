from graph_rag_recommender.model.loadmodel import load_gemini_model
from langchain_core.messages import HumanMessage
from graph_rag_recommender.graph.create_graph import connect_driver, update_user_node
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate
from Chatbot.schemas import FitnessScore
import json

def find_place_and_user_in_graph(driver, user_id, place_id):
    with driver.session() as session:
        style_result = session.run("""
            MATCH (u:User {id: $user_id})-[:HAS_STYLE]->(s:Style)
            OPTIONAL MATCH (u)-[:LIKED]->(p:Place)
            WITH collect(DISTINCT s.name) AS styles, collect(DISTINCT p.category) AS raw_categories
            UNWIND raw_categories AS cat_list
            WITH styles, 
                CASE WHEN apoc.meta.cypher.type(cat_list) = "LIST" THEN cat_list ELSE [cat_list] END AS normalized
            UNWIND normalized AS cat
            RETURN styles, collect(DISTINCT cat) AS liked_categories
        """, user_id=user_id).single()

        styles = style_result["styles"]
        liked_cats = style_result["liked_categories"]
        user_profile = f"The user prefers styles such as {', '.join(styles)} and has liked places in the following categories: {', '.join(liked_cats)}."

        rel_result = session.run("""
            MATCH (u:User {id: $user_id})
            OPTIONAL MATCH (u)-[:LIKED]->(p1:Place)-[:SIMILAR_TO]-(p2:Place {id: $place_id})
            OPTIONAL MATCH (u)-[r:LIKED]->(p2:Place {id: $place_id})
            RETURN count(r) > 0 AS directly_liked, count(p1) > 0 AS similar_liked, collect(DISTINCT p1.name) AS similar_places
        """, user_id=user_id, place_id=place_id).single()

        if rel_result["directly_liked"]:
            relationship_summary = "The user has directly liked this place before."
        elif rel_result["similar_liked"]:
            similar_names = rel_result["similar_places"]
            relationship_summary = f"The user has liked similar places such as: {', '.join(similar_names)}."
        else:
            relationship_summary = "The user has no direct or similar connections to this place."

        if place_id: 
            place_result = session.run("""
                MATCH (p:Place {id: $place_id})
                RETURN p.name AS name, p.category AS category, p.description AS description
            """, place_id = place_id).single()
            place_description = f"{place_result['name']} ({place_result['category']}): {place_result['description']}"
        else: place_description = None
        
        return user_profile, relationship_summary, place_description
        
def format_results_for_llm(records):
    if not records:
        return "No data was returned from the graph."

    formatted = []
    for i, record in enumerate(records):
        lines = [f"[Record {i+1}]"]
        for key, value in record.items():
            if isinstance(value, dict):
                # Flatten node properties
                lines.append(f"{key}: {json.dumps(value, indent=2)}")
            else:
                lines.append(f"{key}: {str(value)}")
        formatted.append("\n".join(lines))

    return "\n\n".join(formatted)


def run_and_format_cypher(driver, cypher_query):
    with driver.session() as session:
        try:
            result = session.run(cypher_query)
            records = result.data()  # List[Dict[str, Any]]
        except Exception as e:
            return f"Failed to run query: {str(e)}"

    return format_results_for_llm(records)


def free_chat_either(user_id, liked_place_ids, styles, place_id, messages):
    driver = connect_driver()
    update_user_node(driver, user_id=user_id, liked_place_ids=liked_place_ids, styles=styles)
    
    chat_history = "\n".join(
        [f"{'User' if isinstance(m, HumanMessage) else 'Assistant'}: {m.content}" for m in messages]
    )

    graph_schema_description = """
    # Graph Schema Overview:
    - (User)-[:HAS_STYLE]->(Style)
    - (User)-[:LIKED]->(Place)
    - (Place)-[:SIMILAR_TO]-(Place)
    - (Place) has properties: id, name, category, description
    - (User) has properties: id
    """

    cypher_prompt = PromptTemplate(
    template="""
        You are a Cypher query generator for a Neo4j travel assistant system.

        Use the following graph structure:
        {graph_schema}

        User info:
        - ID: {user_id}
        - Styles: {styles}
        - Liked Places: {liked_place_ids}
        {place_line}

        Chat history:
        {chat_history}

        If the user's message relates to recommending or evaluating places,
        generate a Cypher query to fetch relevant user/place/style data.

        If the question is not related to the graph, return exactly "NO_CYPHER".
        """,
        input_variables=["graph_schema", "user_id", "styles", "liked_place_ids", "place_line", "chat_history"]
    )
    place_line = f"- The user is asking about place ID: {place_id}" if place_id else ""

    formatted_prompt = cypher_prompt.format(
        graph_schema=graph_schema_description.strip(),
        user_id=user_id,
        styles=styles,
        liked_place_ids=liked_place_ids,
        place_line=place_line,
        chat_history=chat_history
    )

    llm = load_gemini_model()
    cypher_response = llm([HumanMessage(content=formatted_prompt)]).content.strip()
    user_profile, place_description, relationship_summary = find_place_and_user_in_graph(driver, user_id, place_id)

    print(user_profile, "\n")
    print(place_description, "\n")
    print(relationship_summary, "\n")

    if cypher_response == "NO_CYPHER":
        general_prompt = PromptTemplate(
            template="""
            You are a friendly travel assistant.

            Use the following graph data to generate a personalized response to the user's question.

            ## User Information:
            {user_profile}

            ## Place Information (optional):
            {place_description}

            ## Relationship between user and place:
            {relationship_summary}

            ## Chat History:
            {chat_history}

            Please respond in English, naturally and helpfully.
            """,
            input_variables=["user_profile", "place_description", "relationship_summary", "chat_history"]
        )

        response_prompt = general_prompt.format(
            user_profile=user_profile,
            place_description=place_description,
            relationship_summary=relationship_summary,
            chat_history = chat_history
        )
        reply = llm([HumanMessage(content= response_prompt)]).content.strip()
        return {"reply":reply}

    result_text = run_and_format_cypher(driver, cypher_response)

    answer_prompt = PromptTemplate(
        template="""
        You are a travel assistant.

        Use the following user information and graph query result to answer the user's question.
        Respond naturally and helpfully in English.
        If the user does not have any liked categories, generate a response using only their preferred travel styles.

        ## User Information:
        {user_profile}

        ## Place Information (optional):
        {place_description}

        ## Relationship between user and place:
        {relationship_summary}

        Graph data:
        {result_text}

        Conversation so far(optional):
        {chat_history}

        If there is no chat history just return greeting message
        """,
        input_variables=["user_profile", "place_description", "relationship_summary","result_text", "chat_history"]
    )

    final_prompt = answer_prompt.format(
        user_profile = user_profile,
        place_description = place_description,
        relationship_summary = relationship_summary,
        result_text=result_text,
        chat_history=chat_history
    )
    reply = llm([HumanMessage(content=final_prompt)]).content.strip()
    return {"reply": reply}


def fitness_score(user_id, liked_place_ids, styles, place_id):
    driver = connect_driver()
    update_user_node(driver, user_id=user_id, liked_place_ids=liked_place_ids, styles=styles)

    llm = load_gemini_model()
    parser = JsonOutputParser(pydantic_schema= FitnessScore)

    user_profile, place_description, relationship_summary = find_place_and_user_in_graph(driver, user_id, place_id)

    prompt = PromptTemplate(
        template ="""
        You are a travel recommendation assistant. 

        A user has the following profile:
        {user_profile}

        Here is a place: 
        {place_description}

        And here is the relationship between the user and the place:
        {relationship_summary}

        Please evaluate how well this place matches the user's preferences.

        Return your response in JSON format with the following fields:
        - score: integer (0~100)
        - explanation: string

        {format_instructions}
        """,
        input_variables=["user_profile", "place_description", "relationship_summary"],
        partial_variables={"format_instructions": parser.get_format_instructions()}
    )
    
    formatted_prompt = prompt.format(
        user_profile=user_profile,
        place_description=place_description,
        relationship_summary=relationship_summary
    )

    response = llm([HumanMessage(content=formatted_prompt)])
    parsed = parser.parse(response.content)
    return parsed


# def when_to_visit(user_id, liked_place_ids, styles, place_id):
#     driver = connect_driver()
#     update_user_node(driver, user_id=user_id, liked_place_ids=liked_place_ids, styles=styles)

#     llm = loadmodel()
    
#     return  


# def detail_info(user_id, liked_place_ids, styles, place_id):
#     driver = connect_driver()
#     update_user_node(driver, user_id=user_id, liked_place_ids=liked_place_ids, styles=styles)

#     llm = loadmodel()
    
#     return  