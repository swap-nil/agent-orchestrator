.PHONY: test lint opa-test compose-up compose-down run-dev lock helm-sync console evals console-seed \
        helm-lint azure-deploy azure-smoke azure-destroy

test:            ## unit tests (core; no network needed)
	cd tests && python -m unittest discover -v

opa-test:        ## policy tests (requires the opa binary)
	opa test policies/ -v

lint:
	ruff check src tests && mypy src/orchestrator

run-dev:         ## orchestrator with the in-memory dev profile (needs [api] extra)
	PYTHONPATH=src ORCH_CONFIG_FILE=config/orchestrator.dev.yaml python -m orchestrator

compose-up:      ## full local stack
	docker compose -f deploy/docker/docker-compose.yaml up --build -d

compose-down:
	docker compose -f deploy/docker/docker-compose.yaml down -v

LOCK = uv pip compile pyproject.toml --python-version 3.12 --python-platform x86_64-manylinux_2_28 -q
lock:            ## pin exact versions per image (requirements/<EXTRAS>.lock, used by the Dockerfile)
	for x in orchestrator agent token-service mock api; do $(LOCK) --extra $$x -o requirements/$$x.lock; done

HELM_LINT = helm lint -f deploy/helm/examples/global.example.yaml
helm-lint:       ## lint and render every chart with example values (AKS)
	$(HELM_LINT) deploy/helm/platform
	$(HELM_LINT) deploy/helm/domain-agents
	$(HELM_LINT) deploy/helm/orchestrator -f deploy/helm/examples/orchestrator.values.yaml \
	  --set-file config.orchestratorYaml=config/orchestrator.test.yaml --set-file config.intentsYaml=config/intents.yaml \
	  --set-file config.agentsYaml=config/agents.yaml --set-file config.evalsYaml=config/evals.yaml
	$(HELM_LINT) deploy/helm/voice --set-file config.masterAgentYaml=config/master_agent.test.yaml
	$(HELM_LINT) deploy/helm/edge --set-file config.tokenServiceYaml=config/token_service.test.yaml --set certManager.email=ops@example.com

azure-deploy:    ## deploy or update the Azure test environment (see docs/AZURE_TEST_ENV.md)
	deploy/azure/deploy.sh

AZURE_OUTPUT = python3 -c "import json,sys; print(json.load(open('deploy/azure/.out/outputs.json'))[sys.argv[1]]['value'])"
azure-smoke:     ## smoke checks against the deployed test environment
	deploy/azure/scripts/smoke.sh "$$($(AZURE_OUTPUT) appHost)" "$$(. deploy/azure/test.env; echo $${RESOURCE_GROUP:-rg-$$PREFIX})" "$$($(AZURE_OUTPUT) vmName)"

azure-destroy:   ## delete the Azure test environment and its Entra apps
	deploy/azure/destroy.sh

helm-sync:       ## copy the policy into the Helm chart
	cp policies/orchestrator.rego deploy/helm/orchestrator/files/orchestrator.rego

console:         ## command center with the whole stack in one process: http://127.0.0.1:8765/console
	PYTHONPATH=src python -m orchestrator.console.devserver

evals:           ## golden eval suite (fails below the change gate)
	PYTHONPATH=src ORCH_CONFIG_FILE=config/orchestrator.dev.yaml python -m orchestrator.cli run-evals

console-seed:    ## re-embed catalogue, guards and evals into the console's demo mode
	PYTHONPATH=src python scripts/build_console_seed.py
