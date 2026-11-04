DOCKER ?= docker
PY ?= .venv/bin/python
JAR = output_gen/itext_tagger.jar

.PHONY: venv jar test compat fonts fonts-check sample docker

venv:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
	.venv/bin/pip install -r requirements.txt

jar:
	cd output_gen/itext_src && mvn -q clean package $(MVN_FLAGS)
	cp output_gen/itext_src/target/itext-tagger-1.0.0.jar $(JAR)

test:
	$(PY) -m pytest tests/ -q

compat:
	git ls-files '*.py' | xargs $(PY) -c "import sys; from vermin import main; sys.exit(main())" -q --no-tips --violations -t=3.10- --eval-annotations

fonts:
	$(PY) scripts/import_fonts.py $(SRC)

fonts-check:
	$(PY) scripts/import_fonts.py --check

sample:
	$(PY) scripts/make_sample_pdf.py examples/sample.pdf

docker:
	$(DOCKER) build -t readready:dev .
